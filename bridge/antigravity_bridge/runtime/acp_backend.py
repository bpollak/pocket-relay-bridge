"""Antigravity CLI Agent Client Protocol (`agy acp`) JSON-RPC subprocess backend.

Connects directly to the user's signed-in Antigravity Desktop / CLI account via
Apple Keychain unified auth, supporting live streaming thoughts, tokens, tool calls,
interactive permission approval hooks routed to iOS, multi-turn session persistence,
image attachments, and real Antigravity model selection.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from ..models import (
    ApprovalDecision,
    ExecutionPolicy,
    ToolCallRecord,
    ToolCallStatus,
    UsageMetrics,
    utc_now,
)
from ..services.approvals import ApprovalBroker
from .base import AgentTurnCallbacks, AgentTurnRequest, AgentTurnResult


def find_agy_binary() -> str | None:
    found = shutil.which("agy") or shutil.which("agy.exe")
    if found:
        return found
    candidates: list[Path] = [
        Path.home() / ".local" / "bin" / "agy",
        Path("/opt/homebrew/bin/agy"),
        Path("/usr/local/bin/agy"),
    ]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "agy" / "bin" / "agy.exe")
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        candidates.append(Path(user_profile) / "AppData" / "Local" / "agy" / "bin" / "agy.exe")
        candidates.append(Path(user_profile) / ".local" / "bin" / "agy.exe")
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


ACP_STREAM_LIMIT = 10 * 1024 * 1024  # 10 MiB line buffer for large JSON-RPC messages
MACOS_TCC_PROTECTED_SUBDIRS = ("Documents", "Desktop", "Downloads")


def _is_macos_tcc_protected(path: Path) -> bool:
    """Return True if path is inside macOS TCC-gated user folders (Documents/Desktop/Downloads)."""
    if sys.platform != "darwin":
        return False
    try:
        home = Path.home().resolve()
    except OSError:
        home = Path.home()
    for subdir in MACOS_TCC_PROTECTED_SUBDIRS:
        protected = home / subdir
        if path == protected or protected in path.parents:
            return True
    return False


def _probe_dir_readable_sync(path: Path) -> bool:
    """Synchronously check that path exists, is a directory, and can be enumerated."""
    try:
        if not path.exists() or not path.is_dir():
            return False
        with os.scandir(path) as it:
            next(it, None)
        return True
    except OSError:
        return False


async def is_dir_readable(path: Path, timeout: float = 1.0) -> bool:
    """Non-blocking readability probe with a strict timeout so macOS TCC never hangs."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_probe_dir_readable_sync, path),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, OSError):
        return False


class AntigravityACPBackend:
    """Runs Antigravity CLI in ACP JSON-RPC mode (`agy acp`) using unified Antigravity auth."""

    name = "antigravity-acp"

    def __init__(
        self,
        approvals: ApprovalBroker | None = None,
        handshake_timeout: float = 30.0,
    ) -> None:
        self.approvals = approvals
        self.handshake_timeout = handshake_timeout
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._session_ids: dict[str, str] = {}
        self._cancelled_convs: set[str] = set()
        self.discovered_models: list[str] = []

    def is_available(self) -> bool:
        return find_agy_binary() is not None

    def is_authenticated(self) -> bool:
        return self.is_available()

    def reset_session(self, conversation_id: str) -> None:
        self._session_ids.pop(conversation_id, None)

    async def resolve_acp_paths(
        self, workspace_path: str | None
    ) -> tuple[str, str, list[str]]:
        """Resolve (subprocess_cwd, session_cwd, add_dirs) safely without macOS TCC hangs.

        Subprocess `cwd` always stays on `Path.home()` so `launchd` never freezes spawning
        `agy acp` inside TCC-protected folders like `~/Documents`. Accessible non-home
        workspaces are attached via `--add-dir`; inaccessible or TCC-blocked directories
        degrade gracefully to `Path.home()`.
        """
        try:
            home = Path.home().resolve()
        except OSError:
            home = Path.home()
        home_str = str(home)

        if not workspace_path:
            return home_str, home_str, []

        expanded = Path(workspace_path).expanduser()
        try:
            target = expanded.resolve()
        except OSError:
            target = Path(os.path.abspath(str(expanded)))

        if target == home:
            return home_str, home_str, []

        tcc_protected = _is_macos_tcc_protected(target)
        readable = await is_dir_readable(target, timeout=1.0)
        if not readable:
            return home_str, home_str, []

        if tcc_protected:
            # Keep both subprocess cwd and session cwd at home to avoid macOS TCC chdir/walk hangs,
            # while granting access via --add-dir when the directory is readable.
            return home_str, home_str, [str(target)]

        return home_str, str(target), [str(target)]

    async def probe_models(self, cwd: str | None = None) -> list[str]:
        """Query `agy acp` once for the live list of available Antigravity models."""
        agy_bin = find_agy_binary()
        if not agy_bin:
            return []
        subprocess_cwd, session_cwd, _ = await self.resolve_acp_paths(cwd)
        proc = await asyncio.create_subprocess_exec(
            agy_bin,
            "acp",
            cwd=subprocess_cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=ACP_STREAM_LIMIT,
        )
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": 1,
                                "clientInfo": {"name": "antigravity-mobile", "version": "1.0.0"},
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            await proc.stdin.drain()
            await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)

            proc.stdin.write(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "session/new",
                            "params": {"cwd": session_cwd, "mcpServers": []},
                        }
                    )
                    + "\n"
                ).encode()
            )
            await proc.stdin.drain()
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
            msg = json.loads(raw.decode("utf-8", errors="replace"))
            models = (msg.get("result") or {}).get("models", {}).get("availableModels", [])
            ids = [str(m.get("modelId")) for m in models if m.get("modelId")]
            if ids:
                self.discovered_models = ids
            return ids
        except Exception:
            return self.discovered_models
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()

    def _build_prompt_blocks(self, request: AgentTurnRequest) -> list[dict[str, Any]]:
        prompt_text = request.prompt
        if request.system_instructions:
            prompt_text = f"[{request.system_instructions}]\n\n{prompt_text}"
        blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        for att in request.attachments:
            if not att.local_path:
                continue
            p = Path(att.local_path)
            if not p.exists():
                continue
            if att.mime_type.startswith("image/"):
                b64 = base64.b64encode(p.read_bytes()).decode("ascii")
                blocks.append({"type": "image", "data": b64, "mimeType": att.mime_type})
        return blocks

    async def run_turn(
        self,
        request: AgentTurnRequest,
        callbacks: AgentTurnCallbacks,
    ) -> AgentTurnResult:
        agy_bin = find_agy_binary()
        if not agy_bin:
            raise RuntimeError("`agy` CLI is not installed.")

        self._cancelled_convs.discard(request.conversation_id)
        subprocess_cwd, session_cwd, add_dirs = await self.resolve_acp_paths(request.workspace_path)

        cmd = [agy_bin, "acp", "--include-default-customizations"]
        for extra_dir in add_dirs:
            cmd.extend(["--add-dir", extra_dir])
        if request.settings.terminal_sandbox or request.settings.execution_policy == ExecutionPolicy.PROCEED_IN_SANDBOX:
            cmd.append("--sandbox")
        if request.settings.execution_policy == ExecutionPolicy.ALWAYS_PROCEED:
            cmd.append("--dangerously-skip-permissions")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=subprocess_cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=ACP_STREAM_LIMIT,
        )
        self._procs[request.conversation_id] = proc
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

        stderr_buf: list[str] = []

        async def _drain_stderr() -> None:
            total = 0
            try:
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    if total < 16_384:
                        stderr_buf.append(text)
                        total += len(text)
            except Exception:
                return

        stderr_task = asyncio.create_task(_drain_stderr())

        text_parts: list[str] = []
        thought_parts: list[str] = []
        tool_calls: list[ToolCallRecord] = []
        usage: UsageMetrics | None = None
        req_counter = 0

        async def send_rpc(method: str, params: dict[str, Any]) -> int:
            nonlocal req_counter
            req_counter += 1
            rid = req_counter
            payload = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n"
            proc.stdin.write(payload.encode("utf-8"))
            await proc.stdin.drain()
            return rid

        async def send_result(rid: Any, result: dict[str, Any]) -> None:
            payload = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n"
            proc.stdin.write(payload.encode("utf-8"))
            await proc.stdin.drain()

        try:
            init_id = await send_rpc(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "antigravity-mobile", "version": "1.0.0"},
                },
            )
            session_req_id: int | None = None
            prompt_req_id: int | None = None
            prompt_completed = False
            existing_session_id = self._session_ids.get(request.conversation_id)
            active_session_id: str | None = None

            async def dispatch_prompt(sess_id: str) -> None:
                nonlocal prompt_req_id
                # Set mode based on slash command or execution policy
                mode_id = "default"
                if request.slash_command == "/plan":
                    mode_id = "plan"
                elif request.settings.execution_policy == ExecutionPolicy.ALWAYS_PROCEED:
                    mode_id = "auto"
                try:
                    await send_rpc("session/set_mode", {"sessionId": sess_id, "modeId": mode_id})
                except Exception:
                    pass

                # Apply model if it matches a discovered ACP model ID
                if request.settings.model and (
                    not self.discovered_models or request.settings.model in self.discovered_models
                ):
                    try:
                        await send_rpc(
                            "session/set_model",
                            {"sessionId": sess_id, "modelId": request.settings.model},
                        )
                    except Exception:
                        pass

                prompt_req_id = await send_rpc(
                    "session/prompt",
                    {
                        "sessionId": sess_id,
                        "prompt": self._build_prompt_blocks(request),
                    },
                )

            while True:
                if prompt_req_id is None:
                    try:
                        raw_line = await asyncio.wait_for(
                            proc.stdout.readline(),
                            timeout=self.handshake_timeout,
                        )
                    except asyncio.TimeoutError as exc:
                        stderr_preview = "".join(stderr_buf).strip()[:500]
                        detail = f" ({stderr_preview})" if stderr_preview else ""
                        raise RuntimeError(
                            f"`agy acp` timed out during session initialization{detail}"
                        ) from exc
                else:
                    raw_line = await proc.stdout.readline()

                if not raw_line:
                    break
                line_str = raw_line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError:
                    continue

                # 1. Initialize response -> load existing session or create new session
                if msg.get("id") == init_id:
                    if "error" in msg:
                        err = msg["error"]
                        raise RuntimeError(f"ACP initialize error: {err.get('message') if isinstance(err, dict) else err}")
                    if existing_session_id:
                        session_req_id = await send_rpc(
                            "session/load",
                            {"sessionId": existing_session_id, "cwd": session_cwd, "mcpServers": []},
                        )
                    else:
                        session_req_id = await send_rpc(
                            "session/new",
                            {"cwd": session_cwd, "mcpServers": []},
                        )
                    continue

                # 2. Session load/new response
                if session_req_id is not None and msg.get("id") == session_req_id:
                    if "error" in msg:
                        if existing_session_id:
                            # Previous session no longer loadable; create fresh one
                            existing_session_id = None
                            self._session_ids.pop(request.conversation_id, None)
                            session_req_id = await send_rpc(
                                "session/new",
                                {"cwd": session_cwd, "mcpServers": []},
                            )
                            continue
                        err = msg["error"]
                        raise RuntimeError(f"ACP session/new error: {err.get('message') if isinstance(err, dict) else err}")
                    res = msg.get("result") or {}
                    models = (res.get("models") or {}).get("availableModels") or []
                    if models:
                        self.discovered_models = [str(m.get("modelId")) for m in models if m.get("modelId")]
                    active_session_id = res.get("sessionId") or existing_session_id
                    if not active_session_id:
                        raise RuntimeError("ACP session/new returned no sessionId")
                    self._session_ids[request.conversation_id] = active_session_id
                    await dispatch_prompt(active_session_id)
                    continue

                # 3. Server-initiated permission request -> route to iPhone ApprovalBroker!
                if msg.get("method") == "session/request_permission" and "id" in msg:
                    params = msg.get("params") or {}
                    tc = params.get("toolCall") or {}
                    options = params.get("options") or []
                    tool_name = str(tc.get("title") or tc.get("kind") or "tool")
                    raw_input = tc.get("rawInput") or {}
                    args = raw_input if isinstance(raw_input, dict) else {"input": str(raw_input)}

                    chosen_option_id: str | None = None
                    if request.settings.execution_policy == ExecutionPolicy.ALWAYS_PROCEED:
                        allow_opt = next((o for o in options if "allow" in str(o.get("kind", "")).lower()), options[0] if options else None)
                        chosen_option_id = allow_opt.get("optionId") if allow_opt else None
                    elif self.approvals is not None:
                        decision = await self.approvals.request_approval(
                            conversation_id=request.conversation_id,
                            tool_name=tool_name,
                            args=args,
                            reason="Antigravity Desktop/CLI requested approval for this tool.",
                            risk_level="high" if "command" in tool_name.lower() or "bash" in tool_name.lower() else "medium",
                        )
                        if decision == ApprovalDecision.ALLOW_ALWAYS:
                            opt = next((o for o in options if o.get("kind") == "allow_always"), None) or next(
                                (o for o in options if "allow" in str(o.get("kind", "")).lower()), None
                            )
                            chosen_option_id = opt.get("optionId") if opt else None
                        elif decision == ApprovalDecision.ALLOW_ONCE:
                            opt = next((o for o in options if o.get("kind") == "allow_once"), None) or next(
                                (o for o in options if "allow" in str(o.get("kind", "")).lower()), None
                            )
                            chosen_option_id = opt.get("optionId") if opt else None
                        else:
                            opt = next((o for o in options if "reject" in str(o.get("kind", "")).lower()), None)
                            chosen_option_id = opt.get("optionId") if opt else None

                    if chosen_option_id:
                        await send_result(msg["id"], {"outcome": {"outcome": "selected", "optionId": chosen_option_id}})
                    else:
                        await send_result(msg["id"], {"outcome": {"outcome": "cancelled"}})
                    continue

                # 4. Streaming session updates (skip history replay emitted during session/load)
                if msg.get("method") == "session/update":
                    if prompt_req_id is None:
                        continue
                    update = (msg.get("params") or {}).get("update") or {}
                    kind = update.get("sessionUpdate")
                    if kind == "agent_message_chunk":
                        chunk = (update.get("content") or {}).get("text", "")
                        if chunk:
                            text_parts.append(chunk)
                            await callbacks.on_token(chunk)
                    elif kind == "agent_thought_chunk":
                        th = (update.get("content") or {}).get("text", "")
                        if th:
                            thought_parts.append(th)
                            await callbacks.on_thought(th)
                    elif kind == "tool_call":
                        raw_in = update.get("rawInput") or {}
                        rec = ToolCallRecord(
                            id=str(update.get("toolCallId") or f"tool-{len(tool_calls)}"),
                            name=str(update.get("title") or update.get("kind") or "tool"),
                            kind=(str(update.get("kind")).lower() if update.get("kind") else None),
                            args=raw_in if isinstance(raw_in, dict) else {"input": str(raw_in)},
                            status=ToolCallStatus.RUNNING,
                        )
                        tool_calls.append(rec)
                        await callbacks.on_tool_call(rec)
                    elif kind == "tool_call_update":
                        tid = str(update.get("toolCallId") or "")
                        for rec in tool_calls:
                            if rec.id == tid:
                                st = str(update.get("status") or "").lower()
                                if update.get("kind") and not rec.kind:
                                    rec.kind = str(update.get("kind")).lower()
                                content_items = update.get("content") or []
                                if isinstance(content_items, list) and content_items:
                                    texts = [
                                        str((c.get("content") or {}).get("text") or c.get("text") or "")
                                        for c in content_items
                                        if isinstance(c, dict)
                                    ]
                                    joined = "\n".join(t for t in texts if t).strip()
                                    if joined:
                                        rec.output = joined[:4000]
                                if st in ("completed", "done", "success"):
                                    rec.status = ToolCallStatus.COMPLETED
                                    rec.completed_at = utc_now()
                                elif st in ("failed", "error"):
                                    rec.status = ToolCallStatus.FAILED
                                    rec.completed_at = utc_now()
                                elif st in ("denied", "rejected", "cancelled"):
                                    rec.status = ToolCallStatus.DENIED
                                    rec.completed_at = utc_now()
                                await callbacks.on_tool_update(rec)
                                break
                    continue

                # 5. Final session/prompt response
                if prompt_req_id is not None and msg.get("id") == prompt_req_id:
                    if "error" in msg:
                        err = msg["error"]
                        raise RuntimeError(str(err.get("message") if isinstance(err, dict) else err))
                    res = msg.get("result") or {}
                    u = res.get("usage") or {}
                    if u:
                        usage = UsageMetrics(
                            prompt_tokens=int(u.get("inputTokens") or 0),
                            completion_tokens=int(u.get("outputTokens") or 0),
                            total_tokens=int(u.get("totalTokens") or 0),
                        )
                    prompt_completed = True
                    break

            if not prompt_completed and request.conversation_id not in self._cancelled_convs and not text_parts:
                try:
                    await asyncio.wait_for(stderr_task, timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                stderr_preview = "".join(stderr_buf).strip()[:500]
                detail = f": {stderr_preview}" if stderr_preview else ""
                raise RuntimeError(f"`agy acp` exited before completing the turn{detail}")

            return AgentTurnResult(
                content="".join(text_parts),
                thoughts="".join(thought_parts) or None,
                tool_calls=tool_calls,
                usage=usage,
                backend_name=self.name,
            )
        finally:
            self._procs.pop(request.conversation_id, None)
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
            if not stderr_task.done():
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass

    async def cancel(self, conversation_id: str) -> bool:
        self._cancelled_convs.add(conversation_id)
        proc = self._procs.get(conversation_id)
        if proc and proc.returncode is None:
            proc.terminate()
            return True
        return False
