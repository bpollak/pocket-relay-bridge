"""Workspace-aware local fallback backend when Gemini/Vertex credentials or `agy` CLI are not yet configured."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from ..models import (
    ApprovalDecision,
    ExecutionPolicy,
    QuestionItem,
    ToolCallRecord,
    ToolCallStatus,
    UsageMetrics,
    utc_now,
)
from ..services.approvals import ApprovalBroker
from ..services.workspace import WorkspaceService
from .base import AgentTurnCallbacks, AgentTurnRequest, AgentTurnResult


class LocalFallbackBackend:
    """Provides real local workspace inspection, approval-gated shell execution, and artifact previews
    when GEMINI_API_KEY / Vertex / `agy` CLI are not configured yet."""

    name = "local-fallback"

    def __init__(self, approvals: ApprovalBroker, workspace_service: WorkspaceService) -> None:
        self.approvals = approvals
        self.workspace_service = workspace_service
        self._cancelled: set[str] = set()

    def is_available(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return True

    async def run_turn(
        self,
        request: AgentTurnRequest,
        callbacks: AgentTurnCallbacks,
    ) -> AgentTurnResult:
        self._cancelled.discard(request.conversation_id)
        thought_text = (
            "Inspecting active workspace context, attached mentions, and Antigravity runtime status. "
            "No GEMINI_API_KEY, Vertex AI credentials, or `agy` CLI detected in bridge environment, "
            "so executing in local workspace-inspection fallback mode."
        )
        await callbacks.on_thought(thought_text)

        tool_calls: list[ToolCallRecord] = []
        prompt_lower = request.prompt.strip().lower()
        ws_path = Path(request.workspace_path) if request.workspace_path else Path.cwd()

        # Real read-only workspace inspection tool call
        inspect_tool = ToolCallRecord(
            name="list_directory",
            args={"path": str(ws_path)},
            status=ToolCallStatus.RUNNING,
        )
        tool_calls.append(inspect_tool)
        await callbacks.on_tool_call(inspect_tool)

        entries: list[str] = []
        if ws_path.exists() and ws_path.is_dir():
            for child in sorted(ws_path.iterdir())[:20]:
                if child.name.startswith(".git"):
                    continue
                suffix = "/" if child.is_dir() else ""
                entries.append(f"{child.name}{suffix}")
        inspect_tool.status = ToolCallStatus.COMPLETED
        inspect_tool.output = "\n".join(entries) or "(empty directory)"
        inspect_tool.completed_at = utc_now()
        await callbacks.on_tool_update(inspect_tool)

        extra_sections: list[str] = []

        # Interactive approval demonstration when user asks to run a shell command
        if prompt_lower.startswith("run ") or prompt_lower.startswith("/run ") or "run command" in prompt_lower:
            cmd = request.prompt.split(" ", 1)[1].strip() if " " in request.prompt else "git status -sb"
            if cmd.lower().startswith("command "):
                cmd = cmd[8:].strip()
            cmd_tool = ToolCallRecord(
                name="run_command",
                args={"CommandLine": cmd, "cwd": str(ws_path)},
                status=ToolCallStatus.PENDING_APPROVAL,
            )
            tool_calls.append(cmd_tool)
            await callbacks.on_tool_call(cmd_tool)

            approved = True
            if request.settings.execution_policy != ExecutionPolicy.ALWAYS_PROCEED:
                decision = await self.approvals.request_approval(
                    conversation_id=request.conversation_id,
                    tool_name="run_command",
                    args={"CommandLine": cmd, "cwd": str(ws_path)},
                    reason="Terminal command requires approval under current execution policy.",
                    risk_level="high",
                )
                approved = decision in (ApprovalDecision.ALLOW_ONCE, ApprovalDecision.ALLOW_ALWAYS)

            if not approved:
                cmd_tool.status = ToolCallStatus.DENIED
                cmd_tool.error = "Denied by user from Pocket Relay."
                cmd_tool.completed_at = utc_now()
                await callbacks.on_tool_update(cmd_tool)
                extra_sections.append(f"Command `{cmd}` was denied.")
            else:
                cmd_tool.status = ToolCallStatus.RUNNING
                await callbacks.on_tool_update(cmd_tool)
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(ws_path),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                combined = (proc.stdout + proc.stderr).strip()[:4000]
                cmd_tool.status = ToolCallStatus.COMPLETED if proc.returncode == 0 else ToolCallStatus.FAILED
                cmd_tool.output = combined or f"(exit code {proc.returncode})"
                cmd_tool.completed_at = utc_now()
                await callbacks.on_tool_update(cmd_tool)
                extra_sections.append(f"### Command Output (`{cmd}`)\n```text\n{cmd_tool.output}\n```")

        # Interactive AskQuestion demo for /grill-me
        if request.slash_command == "/grill-me":
            answers = await self.approvals.request_question(
                conversation_id=request.conversation_id,
                questions=[
                    QuestionItem(
                        question="Which priority should we optimize for first?",
                        options=[
                            "(Recommended) Ship working end-to-end vertical slice",
                            "Deep architecture & edge-case coverage first",
                            "UI polish & mobile ergonomics first",
                        ],
                        is_multi_select=False,
                    )
                ],
            )
            if answers:
                extra_sections.append(f"Recorded your design choice: `{answers}`.")

        # Generate a real artifact when /plan is invoked
        if request.slash_command == "/plan":
            plan_md = (
                f"# Implementation Plan\n\n"
                f"**Workspace:** `{ws_path.name}`\n\n"
                f"## Objective\n{request.prompt}\n\n"
                f"## Proposed Steps\n"
                f"1. Inspect existing modules in `{ws_path.name}` ({len(entries)} top-level entries found).\n"
                f"2. Implement targeted changes with unit tests.\n"
                f"3. Review diffs in the **Files Changed** inspector tab and verify builds.\n"
            )
            art = self.workspace_service.save_artifact(
                title="implementation_plan.md",
                content=plan_md,
                conversation_id=request.conversation_id,
                kind="plan",
                request_feedback=True,
            )
            extra_sections.append(
                f"Created artifact **`{art.title}`** — open the **Inspector → Artifacts** pane to review or tap **Proceed**."
            )

        body = (
            f"### Pocket Relay Bridge Ready (`{ws_path.name}`)\n\n"
            f"I inspected `{ws_path}` and found **{len(entries)}** top-level items:\n"
            + "\n".join(f"- `{e}`" for e in entries[:10])
            + ("\n\n" + "\n\n".join(extra_sections) if extra_sections else "")
            + "\n\n---\n"
            "> **Connect full Gemini / Antigravity SDK:** Set `GEMINI_API_KEY` (or `GOOGLE_GENAI_USE_VERTEXAI=True` + `GOOGLE_CLOUD_PROJECT`) before launching `start-bridge.sh`, or install the `agy` CLI for ACP mode. "
            "All live workspace tools, diffs, terminals, skills, MCP inspection, approvals, and scheduler features are active now."
        )

        # Stream chunks smoothly so iOS streaming UI is exercised cleanly
        words = body.split(" ")
        for idx in range(0, len(words), 6):
            if request.conversation_id in self._cancelled:
                break
            chunk = " ".join(words[idx : idx + 6]) + (" " if idx + 6 < len(words) else "")
            await callbacks.on_token(chunk)
            await asyncio.sleep(0.015)

        return AgentTurnResult(
            content=body,
            thoughts=thought_text,
            tool_calls=tool_calls,
            usage=UsageMetrics(
                prompt_tokens=max(1, len(request.prompt) // 4),
                completion_tokens=max(1, len(body) // 4),
                total_tokens=max(2, (len(request.prompt) + len(body)) // 4),
            ),
            backend_name=self.name,
        )

    async def cancel(self, conversation_id: str) -> bool:
        self._cancelled.add(conversation_id)
        return True
