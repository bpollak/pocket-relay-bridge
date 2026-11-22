"""Managed background task & interactive terminal service."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..config import BridgeStore
from ..models import StreamEvent, TerminalSession, utc_now

EventEmitter = Callable[[StreamEvent], Awaitable[None]]


class TerminalService:
    def __init__(self, store: BridgeStore, emit: EventEmitter) -> None:
        self.store = store
        self._emit = emit
        self._sessions: dict[str, TerminalSession] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    def list_sessions(self) -> list[TerminalSession]:
        items = list(self._sessions.values())
        items.sort(key=lambda s: s.started_at, reverse=True)
        return items

    def get_session(self, session_id: str) -> TerminalSession | None:
        return self._sessions.get(session_id)

    def running_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.status == "running")

    async def start(self, command: str, workspace_id: str | None = None) -> TerminalSession:
        cwd = str(Path.home())
        if workspace_id and workspace_id in self.store.workspaces:
            candidate = Path(self.store.workspaces[workspace_id].path)
            if candidate.exists() and candidate.is_dir():
                cwd = str(candidate)

        # Respect command denylist
        lower_cmd = command.strip().lower()
        for denied in self.store.settings.command_denylist:
            if denied.strip() and denied.strip().lower() in lower_cmd:
                raise PermissionError(f"Command blocked by denylist rule: {denied}")

        session = TerminalSession(workspace_id=workspace_id, command=command, cwd=cwd)
        self._sessions[session.id] = session

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._processes[session.id] = proc
        await self._emit(
            StreamEvent(type="terminal_started", payload=session.model_dump(mode="json"))
        )
        asyncio.create_task(self._pump_output(session.id, proc))
        return session

    async def _pump_output(self, session_id: str, proc: asyncio.subprocess.Process) -> None:
        session = self._sessions[session_id]
        try:
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(2048)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                session.output = (session.output + text)[-80_000:]
                await self._emit(
                    StreamEvent(
                        type="terminal_output",
                        payload={"id": session_id, "chunk": text},
                    )
                )
            rc = await proc.wait()
            if session.status == "running":
                session.status = "exited"
                session.exit_code = rc
                session.ended_at = utc_now()
        except Exception as exc:
            session.output += f"\n[bridge error: {exc}]\n"
            session.status = "exited"
            session.ended_at = utc_now()
        finally:
            self._processes.pop(session_id, None)
            if session.status != "killed":
                await self._emit(
                    StreamEvent(type="terminal_exited", payload=session.model_dump(mode="json"))
                )

    async def send_input(self, session_id: str, text: str) -> bool:
        proc = self._processes.get(session_id)
        if not proc or not proc.stdin or proc.returncode is not None:
            return False
        data = text if text.endswith("\n") else text + "\n"
        proc.stdin.write(data.encode("utf-8"))
        await proc.stdin.drain()
        return True

    async def kill(self, session_id: str) -> bool:
        proc = self._processes.get(session_id)
        session = self._sessions.get(session_id)
        if not session:
            return False
        if session.status != "running":
            return True  # already finished: no-op, keep exit status
        session.status = "killed"
        session.ended_at = utc_now()
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
        self._processes.pop(session_id, None)
        await self._emit(
            StreamEvent(type="terminal_exited", payload=session.model_dump(mode="json"))
        )
        return True

    async def shutdown(self) -> None:
        for sid in list(self._processes.keys()):
            try:
                await self.kill(sid)
            except Exception:
                pass
