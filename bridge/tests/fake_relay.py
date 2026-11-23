"""A local stand-in for the tunnel relay, with the same forwarding rules as push-relay/src/tunnel.js.

Run it for manual testing:  python tests/fake_relay.py 8799
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import sys

import websockets


class FakeRelay:
    def __init__(self) -> None:
        self.hosts: dict[str, object] = {}
        self.clients: dict[str, dict[int, object]] = {}
        self.next_id = 1
        self.server = None
        self.port = 0
        self.seen_frames: list[bytes] = []

    async def start(self, port: int = 0) -> int:
        self.server = await websockets.serve(self._handle, "127.0.0.1", port, max_size=1024 * 1024)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, ws) -> None:
        parts = ws.request.path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["v1", "tunnel"] or parts[3] not in {"host", "client"}:
            await ws.close(4400, "bad path")
            return
        room, role = parts[2], parts[3]
        if role == "host":
            auth = ws.request.headers.get("Authorization", "")
            secret = auth[7:] if auth.lower().startswith("bearer ") else ""
            if hashlib.sha256(secret.encode()).hexdigest()[:40] != room:
                await ws.close(4401, "not this room's host")
                return
            self.hosts[room] = ws
            try:
                async for message in ws:
                    if isinstance(message, str):
                        control = json.loads(message)
                        target = self.clients.get(room, {}).get(control.get("c"))
                        if control.get("t") == "close" and target is not None:
                            await target.close(4403, "closed by host")
                    elif len(message) > 4:
                        target = self.clients.get(room, {}).get(struct.unpack(">I", message[:4])[0])
                        if target is not None:
                            self.seen_frames.append(message[4:])
                            await target.send(message[4:])
            finally:
                if self.hosts.get(room) is ws:
                    del self.hosts[room]
                for client in list(self.clients.get(room, {}).values()):
                    await client.close(4410, "host went away")
            return

        host = self.hosts.get(room)
        if host is None:
            await ws.close(4404, "host offline")
            return
        client_id = self.next_id
        self.next_id += 1
        self.clients.setdefault(room, {})[client_id] = ws
        await host.send(json.dumps({"t": "open", "c": client_id}))
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    self.seen_frames.append(message)
                    await host.send(struct.pack(">I", client_id) + message)
        finally:
            self.clients.get(room, {}).pop(client_id, None)
            current = self.hosts.get(room)
            if current is not None:
                try:
                    await current.send(json.dumps({"t": "close", "c": client_id}))
                except Exception:
                    pass


async def _main(port: int) -> None:
    relay = FakeRelay()
    print(f"fake relay on ws://127.0.0.1:{await relay.start(port)}", flush=True)
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(_main(int(sys.argv[1]) if len(sys.argv) > 1 else 8799))
