"""Remote access: lets a paired phone reach this bridge when it is not on the same network.

The bridge keeps one outbound WebSocket open to the Pocket Relay tunnel relay. A phone connects to
the same relay "room", and the relay passes sealed records between the two. The relay never holds
the key, so it cannot read or change what passes through it.

Per phone connection:

1. Each side sends a fresh X25519 public key.
2. Both derive two ChaCha20-Poly1305 keys with HKDF-SHA256, salted with the 32-byte tunnel key
   that the phone received when it scanned the pairing QR code. Without that key nothing decrypts,
   which is what authenticates both ends; the fresh keys give forward secrecy.
3. Every later record is `0x03 || AEAD(counter nonce, flags || chunk)`.

Inside the tunnel the phone sends ordinary API requests, which are answered by this same bridge
application in-process. The bearer token is still required on every request, and only `/api/`
routes (not the pairing routes) and `/health` can be reached this way.

Set POCKET_RELAY_REMOTE=0 to turn remote access off, or POCKET_RELAY_TUNNEL_URL to use a relay you
run yourself.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import struct
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

logger = logging.getLogger("antigravity_bridge.tunnel")

DEFAULT_RELAY_URL = "wss://relay.pocketrelay.app"
PROTOCOL_INFO = b"pocket-relay-tunnel-v1"
MSG_CLIENT_HELLO = 0x01
MSG_HOST_HELLO = 0x02
MSG_RECORD = 0x03
FLAG_MORE = 0x01
CHUNK_BYTES = 32 * 1024
MAX_MESSAGE_BYTES = 48 * 1024 * 1024
MAX_CLIENTS = 8
#: Routes a phone may call through the tunnel. Pairing pages and pairing-code routes are
#: deliberately left out: they are for someone sitting at this computer.
ALLOWED_PREFIXES = ("/api/",)
BLOCKED_PREFIXES = ("/api/pair/", "/api/pairing")
FORWARDED_HEADERS = ("authorization", "content-type")


# ----------------------------------------------------------------------------- crypto


def derive_keys(shared: bytes, tunnel_key: bytes, client_pub: bytes, host_pub: bytes) -> tuple[bytes, bytes]:
    """Returns (client-to-host key, host-to-client key)."""
    okm = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=tunnel_key,
        info=PROTOCOL_INFO + client_pub + host_pub,
    ).derive(shared)
    return okm[:32], okm[32:]


def _nonce(counter: int) -> bytes:
    return b"\x00\x00\x00\x00" + struct.pack(">Q", counter)


class Sealer:
    """Encrypts one direction of a session. Each record uses the next counter value exactly once."""

    def __init__(self, key: bytes) -> None:
        self._aead = ChaCha20Poly1305(key)
        self._counter = 0

    def seal_message(self, message: bytes) -> list[bytes]:
        """Splits a message into sealed records small enough for any relay."""
        records: list[bytes] = []
        chunks = [message[i : i + CHUNK_BYTES] for i in range(0, len(message), CHUNK_BYTES)] or [b""]
        for index, chunk in enumerate(chunks):
            flags = FLAG_MORE if index < len(chunks) - 1 else 0
            sealed = self._aead.encrypt(_nonce(self._counter), bytes([flags]) + chunk, None)
            self._counter += 1
            records.append(bytes([MSG_RECORD]) + sealed)
        return records


class Opener:
    """Decrypts one direction of a session and joins fragments back into messages."""

    def __init__(self, key: bytes) -> None:
        self._aead = ChaCha20Poly1305(key)
        self._counter = 0
        self._parts: list[bytes] = []
        self._size = 0

    def open_record(self, record: bytes) -> bytes | None:
        """Returns a whole message once its last fragment arrives. Raises InvalidTag on tampering."""
        if not record or record[0] != MSG_RECORD:
            raise InvalidTag()
        plain = self._aead.decrypt(_nonce(self._counter), record[1:], None)
        self._counter += 1
        if not plain:
            raise InvalidTag()
        self._parts.append(plain[1:])
        self._size += len(plain) - 1
        if self._size > MAX_MESSAGE_BYTES:
            raise ValueError("Tunnel message too large")
        if plain[0] & FLAG_MORE:
            return None
        message = b"".join(self._parts)
        self._parts, self._size = [], 0
        return message


class HostSession:
    """The bridge's half of one phone connection."""

    def __init__(self, tunnel_key: bytes, private_key: X25519PrivateKey | None = None) -> None:
        self._tunnel_key = tunnel_key
        self._private = private_key or X25519PrivateKey.generate()
        self.sealer: Sealer | None = None
        self.opener: Opener | None = None
        self.subscribed = False
        self.send_lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self.sealer is not None

    def accept_hello(self, hello: bytes) -> bytes:
        """Takes the phone's hello and returns ours. After this, records can flow."""
        if len(hello) != 33 or hello[0] != MSG_CLIENT_HELLO:
            raise ValueError("Bad tunnel hello")
        client_pub = hello[1:]
        host_pub = self._private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        shared = self._private.exchange(X25519PublicKey.from_public_bytes(client_pub))
        client_to_host, host_to_client = derive_keys(shared, self._tunnel_key, client_pub, host_pub)
        self.opener = Opener(client_to_host)
        self.sealer = Sealer(host_to_client)
        return bytes([MSG_HOST_HELLO]) + host_pub


# ----------------------------------------------------------------------------- credentials


class TunnelCredentials:
    """The secrets that identify this bridge to the relay and to paired phones."""

    def __init__(self, data_dir: Path) -> None:
        self._file = Path(data_dir) / "tunnel.json"
        self.host_secret = ""
        self.tunnel_key = b""
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
            self.host_secret = str(raw["host_secret"])
            self.tunnel_key = base64.urlsafe_b64decode(raw["tunnel_key"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if len(self.host_secret) < 32 or len(self.tunnel_key) != 32:
            self.rotate()

    def rotate(self) -> None:
        """New room and key. Phones paired before this can no longer connect remotely."""
        self.host_secret = secrets.token_urlsafe(32)
        self.tunnel_key = secrets.token_bytes(32)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({
                "host_secret": self.host_secret,
                "tunnel_key": base64.urlsafe_b64encode(self.tunnel_key).decode("ascii"),
            }),
            encoding="utf-8",
        )
        tmp.replace(self._file)
        try:
            os.chmod(self._file, 0o600)
        except OSError:
            pass

    @property
    def room_id(self) -> str:
        """Public name of this bridge's room. Only the holder of `host_secret` can be its host."""
        return hashlib.sha256(self.host_secret.encode("utf-8")).hexdigest()[:40]

    @property
    def tunnel_key_text(self) -> str:
        return base64.urlsafe_b64encode(self.tunnel_key).decode("ascii").rstrip("=")


# ----------------------------------------------------------------------------- service


def is_allowed_path(path: str) -> bool:
    bare = path.split("?", 1)[0]
    if bare == "/health":
        return True
    if "/../" in bare or bare.endswith("/..") or "//" in bare:
        return False
    return bare.startswith(ALLOWED_PREFIXES) and not bare.startswith(BLOCKED_PREFIXES)


class _EventTap:
    """Stands in for a WebSocket in the bridge's event hub, sending events down one tunnel session."""

    def __init__(self, send: Callable[[str], Awaitable[None]]) -> None:
        self._send = send

    async def send_text(self, payload: str) -> None:
        await self._send(payload)


class TunnelService:
    def __init__(
        self,
        data_dir: Path,
        app: Any,
        hub: Any,
        verify_token: Callable[[str | None], bool],
        connected_event: Callable[[], str],
    ) -> None:
        self.credentials = TunnelCredentials(data_dir)
        self.relay_url = os.environ.get("POCKET_RELAY_TUNNEL_URL", DEFAULT_RELAY_URL).strip().rstrip("/")
        self.enabled = (
            os.environ.get("POCKET_RELAY_REMOTE", "1").strip().lower() not in {"0", "false", "off", "no"}
            and bool(self.relay_url)
        )
        self._app = app
        self._hub = hub
        self._verify_token = verify_token
        self._connected_event = connected_event
        self._sessions: dict[int, HostSession] = {}
        self._taps: dict[int, _EventTap] = {}
        self._task: asyncio.Task[None] | None = None
        self._socket: Any = None
        self._http: httpx.AsyncClient | None = None
        self._request_tasks: set[asyncio.Task[None]] = set()
        self.connected = False

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._drop_all()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def restart(self) -> None:
        """Reconnects under the current credentials (used after they are rotated)."""
        await self.stop()
        self.start()

    def _drop_all(self) -> None:
        for client_id in list(self._sessions):
            self._drop(client_id)
        self.connected = False

    def _drop(self, client_id: int) -> None:
        self._sessions.pop(client_id, None)
        tap = self._taps.pop(client_id, None)
        if tap is not None:
            self._hub.disconnect(tap)

    async def _run(self) -> None:
        import websockets

        url = f"{self.relay_url}/v1/tunnel/{self.credentials.room_id}/host"
        headers = {"Authorization": f"Bearer {self.credentials.host_secret}"}
        delay = 1.0
        while True:
            try:
                async with websockets.connect(
                    url, additional_headers=headers, max_size=1024 * 1024, ping_interval=25, ping_timeout=20
                ) as socket:
                    self._socket = socket
                    self.connected = True
                    delay = 1.0
                    async for message in socket:
                        await self._on_relay_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("Remote access relay is not reachable (%s); retrying.", type(exc).__name__)
            finally:
                self._socket = None
                self._drop_all()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    # -------------------------------------------------------------- relay frames

    async def _on_relay_message(self, message: str | bytes) -> None:
        if isinstance(message, str):
            try:
                control = json.loads(message)
            except ValueError:
                return
            client_id = control.get("c")
            if not isinstance(client_id, int):
                return
            if control.get("t") == "open":
                if len(self._sessions) >= MAX_CLIENTS:
                    await self._close_client(client_id)
                else:
                    self._sessions[client_id] = HostSession(self.credentials.tunnel_key)
            elif control.get("t") == "close":
                self._drop(client_id)
            return

        if len(message) < 5:
            return
        client_id = struct.unpack(">I", message[:4])[0]
        frame = message[4:]
        session = self._sessions.get(client_id)
        if session is None:
            return
        try:
            if not session.ready:
                await self._send_frame(client_id, session.accept_hello(frame))
                return
            assert session.opener is not None
            plain = session.opener.open_record(frame)
        except (InvalidTag, ValueError):
            # Wrong key or a tampered record: this phone does not hold the tunnel key.
            self._drop(client_id)
            await self._close_client(client_id)
            return
        if plain is not None:
            self._spawn(self._on_message(client_id, session, plain))

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]
        self._request_tasks.add(task)
        task.add_done_callback(self._request_tasks.discard)

    async def _send_frame(self, client_id: int, frame: bytes) -> None:
        socket = self._socket
        if socket is not None:
            await socket.send(struct.pack(">I", client_id) + frame)

    async def _close_client(self, client_id: int) -> None:
        socket = self._socket
        if socket is not None:
            try:
                await socket.send(json.dumps({"t": "close", "c": client_id}))
            except Exception:
                pass

    async def _send_message(self, client_id: int, session: HostSession, message: dict[str, Any]) -> None:
        if session.sealer is None:
            return
        data = json.dumps(message, separators=(",", ":")).encode("utf-8")
        # One message at a time per phone: record counters must go out in order.
        async with session.send_lock:
            for record in session.sealer.seal_message(data):
                await self._send_frame(client_id, record)

    # -------------------------------------------------------------- inner protocol

    async def _on_message(self, client_id: int, session: HostSession, plain: bytes) -> None:
        try:
            message = json.loads(plain)
        except ValueError:
            return
        kind = message.get("t")
        if kind == "req":
            await self._send_message(client_id, session, await self.handle_request(message))
        elif kind == "sub":
            if not self._verify_token(message.get("token")):
                await self._send_message(client_id, session, {"t": "sub", "ok": False})
                return
            await self._send_message(client_id, session, {"t": "sub", "ok": True})
            await self._send_message(client_id, session, {"t": "ev", "event": self._connected_event()})
            if not session.subscribed:
                session.subscribed = True

                async def forward(payload: str, cid: int = client_id, sess: HostSession = session) -> None:
                    await self._send_message(cid, sess, {"t": "ev", "event": payload})

                tap = _EventTap(forward)
                self._taps[client_id] = tap
                self._hub.add_listener(tap)
        elif kind == "ping":
            await self._send_message(client_id, session, {"t": "pong"})

    async def handle_request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Answers one tunnelled API request using this bridge's own application."""
        request_id = message.get("id")
        path = str(message.get("path") or "")
        method = str(message.get("method") or "GET").upper()
        if not is_allowed_path(path) or method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            return {"t": "res", "id": request_id, "status": 403, "headers": {"content-type": "application/json"},
                    "body": base64.b64encode(b'{"detail":"Not available over remote access"}').decode("ascii")}
        headers = {
            name: str(value)
            for name, value in (message.get("headers") or {}).items()
            if name.lower() in FORWARDED_HEADERS
        }
        body = base64.b64decode(message["body"]) if message.get("body") else None
        if self._http is None:
            # The client address is a documentation-only address, never loopback, so routes that
            # trust requests from this computer itself do not trust the tunnel.
            transport = httpx.ASGITransport(app=self._app, client=("192.0.2.1", 0))
            self._http = httpx.AsyncClient(transport=transport, base_url="http://tunnel.invalid", timeout=120.0)
        try:
            response = await self._http.request(method, path, headers=headers, content=body)
            return {
                "t": "res",
                "id": request_id,
                "status": response.status_code,
                "headers": {"content-type": response.headers.get("content-type", "application/json")},
                "body": base64.b64encode(response.content).decode("ascii"),
            }
        except Exception as exc:
            logger.warning("Tunnelled request failed: %s", type(exc).__name__)
            return {"t": "res", "id": request_id, "status": 502, "headers": {"content-type": "application/json"},
                    "body": base64.b64encode(b'{"detail":"The bridge could not answer"}').decode("ascii")}
