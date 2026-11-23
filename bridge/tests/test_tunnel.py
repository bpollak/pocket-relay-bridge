"""Remote access tunnel: encryption, what can be reached through it, and a full round trip."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest
import websockets
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from antigravity_bridge.main import create_app
from antigravity_bridge.models import RuntimeMode, StreamEvent
from antigravity_bridge.runtime.acp_backend import AntigravityACPBackend
from antigravity_bridge.services.tunnel import (
    CHUNK_BYTES,
    HostSession,
    Opener,
    Sealer,
    TunnelCredentials,
    derive_keys,
    is_allowed_path,
)
from fake_relay import FakeRelay


class PhoneSession:
    """The phone's half of the handshake, written independently of HostSession."""

    def __init__(self, tunnel_key: bytes, private_key: X25519PrivateKey | None = None) -> None:
        self.tunnel_key = tunnel_key
        self.private = private_key or X25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.sealer: Sealer | None = None
        self.opener: Opener | None = None

    def hello(self) -> bytes:
        return b"\x01" + self.public

    def finish(self, host_hello: bytes) -> None:
        assert host_hello[0] == 0x02 and len(host_hello) == 33
        shared = self.private.exchange(X25519PublicKey.from_public_bytes(host_hello[1:]))
        c2h, h2c = derive_keys(shared, self.tunnel_key, self.public, host_hello[1:])
        self.sealer, self.opener = Sealer(c2h), Opener(h2c)


def test_handshake_round_trip_fragmentation_and_tamper_detection():
    key = bytes(range(32))
    host, phone = HostSession(key), PhoneSession(key)
    phone.finish(host.accept_hello(phone.hello()))

    big = b"x" * (CHUNK_BYTES * 2 + 17)
    records = phone.sealer.seal_message(big)
    assert len(records) == 3
    assert all(b"xxxx" not in r for r in records), "records are encrypted"
    results = [host.opener.open_record(r) for r in records]
    assert results[:2] == [None, None] and results[2] == big

    reply = host.sealer.seal_message(b'{"ok":true}')
    assert phone.opener.open_record(reply[0]) == b'{"ok":true}'

    tampered = bytearray(host.sealer.seal_message(b"hello")[0])
    tampered[-1] ^= 1
    with pytest.raises(InvalidTag):
        phone.opener.open_record(bytes(tampered))


def test_replayed_and_reordered_records_are_rejected():
    key = bytes(range(32))
    host, phone = HostSession(key), PhoneSession(key)
    phone.finish(host.accept_hello(phone.hello()))
    first = phone.sealer.seal_message(b"one")[0]
    second = phone.sealer.seal_message(b"two")[0]
    with pytest.raises(InvalidTag):
        host.opener.open_record(second)  # out of order

    host2, phone2 = HostSession(key), PhoneSession(key)
    phone2.finish(host2.accept_hello(phone2.hello()))
    record = phone2.sealer.seal_message(b"once")[0]
    assert host2.opener.open_record(record) == b"once"
    with pytest.raises(InvalidTag):
        host2.opener.open_record(record)  # replay
    assert first  # silence unused


def test_wrong_tunnel_key_cannot_talk():
    host, phone = HostSession(b"\x01" * 32), PhoneSession(b"\x02" * 32)
    phone.finish(host.accept_hello(phone.hello()))
    with pytest.raises(InvalidTag):
        host.opener.open_record(phone.sealer.seal_message(b"hi")[0])


def test_only_api_routes_are_reachable():
    for path in ["/api/status", "/api/conversations/abc/messages?x=1", "/health"]:
        assert is_allowed_path(path), path
    for path in ["/pair", "/pair/prompt", "/api/pair/exchange", "/api/pair/request", "/api/pairing",
                 "/api/pairing/rotate", "/api/pairing/qr.png", "/ws", "/", "/docs", "/openapi.json",
                 "/api/../pair", "/api//pair", "api/status"]:
        assert not is_allowed_path(path), path


def test_credentials_persist_and_rotate(tmp_path: Path):
    first = TunnelCredentials(tmp_path)
    again = TunnelCredentials(tmp_path)
    assert (again.host_secret, again.tunnel_key) == (first.host_secret, first.tunnel_key)
    assert len(first.room_id) == 40 and first.host_secret not in first.room_id
    old_room = first.room_id
    first.rotate()
    assert first.room_id != old_room and TunnelCredentials(tmp_path).room_id == first.room_id


def test_interop_vector_matches_the_one_the_app_tests_use():
    """Fixed inputs so the Swift implementation can be checked against this one byte for byte."""
    vector = json.loads((Path(__file__).parent / "tunnel_vector.json").read_text())
    key = bytes.fromhex(vector["tunnel_key"])
    host = HostSession(key, X25519PrivateKey.from_private_bytes(bytes.fromhex(vector["host_private"])))
    phone = PhoneSession(key, X25519PrivateKey.from_private_bytes(bytes.fromhex(vector["client_private"])))
    host_hello = host.accept_hello(phone.hello())
    phone.finish(host_hello)
    assert phone.hello().hex() == vector["client_hello"]
    assert host_hello.hex() == vector["host_hello"]
    assert [r.hex() for r in phone.sealer.seal_message(vector["client_message"].encode())] == vector["client_records"]
    assert [r.hex() for r in host.sealer.seal_message(vector["host_message"].encode())] == vector["host_records"]


@pytest.fixture()
async def tunnelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    relay = FakeRelay()
    port = await relay.start()
    monkeypatch.setenv("POCKET_RELAY_REMOTE", "1")
    monkeypatch.setenv("POCKET_RELAY_TUNNEL_URL", f"ws://127.0.0.1:{port}")
    monkeypatch.setenv("ANTIGRAVITY_DEFAULT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ANTIGRAVITY_DISABLE_BONJOUR", "1")
    monkeypatch.setattr(AntigravityACPBackend, "is_available", lambda self: False)
    app = create_app(data_dir=tmp_path / ".bridge_data", port=8765)
    app.state.store.settings.runtime_mode = RuntimeMode.LOCAL_FALLBACK
    tunnel = app.state.tunnel
    tunnel.start()
    for _ in range(100):
        if tunnel.connected:
            break
        await asyncio.sleep(0.05)
    assert tunnel.connected
    try:
        yield app, relay, port
    finally:
        await tunnel.stop()
        await relay.stop()


class Phone:
    def __init__(self, ws, session: PhoneSession) -> None:
        self.ws, self.session, self.next_id = ws, session, 1

    @classmethod
    async def connect(cls, port: int, room: str, key: bytes) -> "Phone":
        ws = await websockets.connect(f"ws://127.0.0.1:{port}/v1/tunnel/{room}/client")
        session = PhoneSession(key)
        await ws.send(session.hello())
        session.finish(await asyncio.wait_for(ws.recv(), 5))
        return cls(ws, session)

    async def send(self, message: dict) -> None:
        for record in self.session.sealer.seal_message(json.dumps(message).encode()):
            await self.ws.send(record)

    async def receive(self) -> dict:
        while True:
            plain = self.session.opener.open_record(await asyncio.wait_for(self.ws.recv(), 5))
            if plain is not None:
                return json.loads(plain)

    async def request(self, method: str, path: str, token: str | None = None, body: bytes | None = None,
                      content_type: str | None = None) -> tuple[int, bytes]:
        request_id = self.next_id
        self.next_id += 1
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if content_type:
            headers["Content-Type"] = content_type
        await self.send({"t": "req", "id": request_id, "method": method, "path": path, "headers": headers,
                         "body": base64.b64encode(body).decode() if body else None})
        while True:
            reply = await self.receive()
            if reply.get("t") == "res" and reply.get("id") == request_id:
                return reply["status"], base64.b64decode(reply["body"])


async def test_phone_reaches_the_api_through_the_relay(tunnelled):
    app, relay, port = tunnelled
    creds, token = app.state.tunnel.credentials, app.state.store.auth_token
    phone = await Phone.connect(port, creds.room_id, creds.tunnel_key)

    status, body = await phone.request("GET", "/api/status", token)
    assert status == 200 and json.loads(body)["host_id"] == app.state.store.host_id

    assert (await phone.request("GET", "/api/status"))[0] == 401, "the bearer token is still required"
    assert (await phone.request("GET", "/api/status", "wrong"))[0] == 401
    for path in ["/pair", "/api/pairing", "/api/pair/request"]:
        assert (await phone.request("GET", path, token))[0] == 403, path

    # A large upload crosses the tunnel in fragments and arrives intact.
    payload = bytes(range(256)) * 400
    boundary = "b123"
    multipart = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="big.bin"\r\n'
                 f"Content-Type: application/octet-stream\r\n\r\n").encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    status, body = await phone.request("POST", "/api/uploads", token, multipart, f"multipart/form-data; boundary={boundary}")
    assert status == 200 and json.loads(body)["size_bytes"] == len(payload)

    # The relay only ever saw sealed records: none of the API text, and not the token.
    seen = b"".join(relay.seen_frames)
    assert b"host_id" not in seen and token.encode() not in seen and b"/api/status" not in seen
    await phone.ws.close()


async def test_live_events_need_the_token_and_then_stream(tunnelled):
    app, _, port = tunnelled
    creds, token = app.state.tunnel.credentials, app.state.store.auth_token
    phone = await Phone.connect(port, creds.room_id, creds.tunnel_key)

    await phone.send({"t": "sub", "token": "wrong"})
    assert await phone.receive() == {"t": "sub", "ok": False}
    await app.state.hub.broadcast(StreamEvent(type="token", payload={"delta": "not for you"}))

    await phone.send({"t": "sub", "token": token})
    assert await phone.receive() == {"t": "sub", "ok": True}
    connected = await phone.receive()
    assert connected["t"] == "ev" and json.loads(connected["event"])["type"] == "connected"

    await app.state.hub.broadcast(StreamEvent(type="token", conversation_id="c1", payload={"delta": "hello"}))
    event = await phone.receive()
    assert json.loads(event["event"])["payload"] == {"delta": "hello"}

    await phone.ws.close()
    for _ in range(100):
        if not app.state.hub._clients:
            break
        await asyncio.sleep(0.05)
    assert not app.state.hub._clients, "a phone that leaves stops receiving events"


async def test_wrong_key_or_wrong_host_secret_is_turned_away(tunnelled):
    app, _, port = tunnelled
    creds = app.state.tunnel.credentials

    stranger = await websockets.connect(f"ws://127.0.0.1:{port}/v1/tunnel/{creds.room_id}/client")
    session = PhoneSession(b"\x07" * 32)
    await stranger.send(session.hello())
    session.finish(await asyncio.wait_for(stranger.recv(), 5))
    await stranger.send(session.sealer.seal_message(b'{"t":"req","id":1,"method":"GET","path":"/api/status"}')[0])
    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(stranger.recv(), 5)

    impostor = await websockets.connect(f"ws://127.0.0.1:{port}/v1/tunnel/{creds.room_id}/host",
                                        additional_headers={"Authorization": "Bearer not-the-secret"})
    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(impostor.recv(), 5)


def test_pairing_link_carries_remote_access_only_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ANTIGRAVITY_DISABLE_BONJOUR", "1")
    monkeypatch.setattr(AntigravityACPBackend, "is_available", lambda self: False)
    app = create_app(data_dir=tmp_path / "off", port=8765)
    headers = {"Authorization": f"Bearer {app.state.store.auth_token}"}
    with TestClient(app) as client:
        info = client.get("/api/pairing", headers=headers).json()
        assert info["relay_url"] is None and "rk=" not in info["pairing_uri"]

    monkeypatch.setenv("POCKET_RELAY_REMOTE", "1")
    monkeypatch.setenv("POCKET_RELAY_TUNNEL_URL", "ws://127.0.0.1:9")
    app = create_app(data_dir=tmp_path / "on", port=8765)
    headers = {"Authorization": f"Bearer {app.state.store.auth_token}"}
    with TestClient(app) as client:
        info = client.get("/api/pairing", headers=headers).json()
        creds = app.state.tunnel.credentials
        assert info["relay_room"] == creds.room_id and info["relay_key"] == creds.tunnel_key_text
        assert f"room={creds.room_id}" in info["pairing_uri"] and "rk=" in info["pairing_uri"]
        old_room = creds.room_id
        client.post("/api/pairing/rotate", headers={"Authorization": f"Bearer {app.state.store.auth_token}"})
        assert app.state.tunnel.credentials.room_id != old_room
