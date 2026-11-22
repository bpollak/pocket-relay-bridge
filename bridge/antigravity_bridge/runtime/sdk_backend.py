"""Official Google Antigravity Python SDK backend."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..config import BridgeStore
from ..models import (
    AccessPolicy,
    AgentBehaviorMode,
    ApprovalDecision,
    ExecutionPolicy,
    PermissionGrant,
    ToolCallRecord,
    ToolCallStatus,
    UsageMetrics,
    utc_now,
)
from ..services.approvals import ApprovalBroker
from ..services.customizations import CustomizationsService
from .base import AgentTurnCallbacks, AgentTurnRequest, AgentTurnResult

try:
    import google.antigravity as ag
    from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig, types
    from google.antigravity.hooks import policy as ag_policy

    SDK_IMPORTABLE = True
except Exception:  # pragma: no cover
    ag = None  # type: ignore[assignment]
    Agent = None  # type: ignore[assignment]
    CapabilitiesConfig = None  # type: ignore[assignment]
    LocalAgentConfig = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    ag_policy = None  # type: ignore[assignment]
    SDK_IMPORTABLE = False


class AntigravitySDKBackend:
    """Executes turns through google-antigravity SDK with live streaming and iOS approval hooks."""

    name = "antigravity-sdk"

    def __init__(
        self,
        store: BridgeStore,
        approvals: ApprovalBroker,
        customizations: CustomizationsService,
    ) -> None:
        self.store = store
        self.approvals = approvals
        self.customizations = customizations
        # Set by the app: awaited after this backend mutates GlobalSettings.
        self.on_settings_changed: Callable[[], Awaitable[None]] | None = None
        self._active_responses: dict[str, Any] = {}

    def is_available(self) -> bool:
        return SDK_IMPORTABLE

    def is_authenticated(self) -> bool:
        if not SDK_IMPORTABLE:
            return False
        if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            return True
        if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true", "yes"):
            return bool(os.environ.get("GOOGLE_CLOUD_PROJECT"))
        return False

    def _build_mcp_servers(self) -> list[Any]:
        if not SDK_IMPORTABLE or types is None:
            return []
        result: list[Any] = []
        for srv in self.customizations.discover_mcp_servers():
            if not srv.enabled or not srv.command_or_url:
                continue
            try:
                if srv.transport in ("http", "sse"):
                    result.append(
                        types.McpStreamableHttpServer(name=srv.name, url=srv.command_or_url)
                    )
                else:
                    result.append(
                        types.McpStdioServer(
                            name=srv.name,
                            command=srv.command_or_url,
                            args=srv.args,
                        )
                    )
            except Exception:
                continue
        return result

    async def _record_always_allow(self, tool_name: str, cmd_line: str) -> None:
        """Persist an "Always allow" grant and notify clients that settings changed."""
        if tool_name == "run_command" and cmd_line.strip():
            prefix = cmd_line.strip().split()[0]
            if prefix not in self.store.settings.command_allowlist:
                self.store.settings.command_allowlist.append(prefix)
        else:
            self.store.settings.permission_grants.append(
                PermissionGrant(kind="tool", pattern=tool_name, effect="allow")
            )
        self.store.save()
        if self.on_settings_changed is not None:
            try:
                await self.on_settings_changed()
            except Exception:
                pass

    def _build_policies(self, request: AgentTurnRequest) -> list[Any]:
        if not SDK_IMPORTABLE or ag_policy is None:
            return []
        eff = request.settings
        policies: list[Any] = []

        async def approval_handler(tool_call: Any) -> bool:
            tool_name = getattr(tool_call, "name", str(tool_call))
            raw_args = getattr(tool_call, "args", {}) or {}
            args = dict(raw_args) if isinstance(raw_args, dict) else {"raw": str(raw_args)}

            # Check command allowlist/denylist for run_command
            cmd_line = str(args.get("CommandLine") or args.get("command") or "")
            if tool_name == "run_command" and cmd_line:
                for denied in eff.command_denylist:
                    if denied.strip() and denied.strip().lower() in cmd_line.lower():
                        return False
                for allowed in eff.command_allowlist:
                    if allowed.strip() and cmd_line.strip().lower().startswith(allowed.strip().lower()):
                        return True

            decision = await self.approvals.request_approval(
                conversation_id=request.conversation_id,
                tool_name=tool_name,
                args=args,
                reason=f"Tool '{tool_name}' requires confirmation ({eff.execution_policy.value}).",
                risk_level="high" if tool_name == "run_command" else "medium",
            )
            if decision == ApprovalDecision.ALLOW_ALWAYS:
                await self._record_always_allow(tool_name, cmd_line)
                return True
            return decision == ApprovalDecision.ALLOW_ONCE

        if request.workspace_path and eff.non_workspace_file_access == AccessPolicy.DENY:
            policies.append(ag_policy.workspace_only(request.workspace_path))

        if eff.execution_policy == ExecutionPolicy.ALWAYS_PROCEED:
            policies.append(ag_policy.allow_all())
        elif eff.execution_policy == ExecutionPolicy.STRICT:
            policies.append(ag_policy.deny("*"))
            for ro in ("list_directory", "search_directory", "find_file", "view_file", "finish"):
                policies.append(ag_policy.allow(ro))
            for mut in ("create_file", "edit_file", "run_command", "search_web", "read_url_content"):
                policies.append(ag_policy.ask_user(mut, handler=approval_handler))
        else:
            # REQUEST_REVIEW or PROCEED_IN_SANDBOX
            policies.append(ag_policy.safe_defaults(handler=approval_handler))

        if eff.internet_access_policy == AccessPolicy.DENY:
            policies.append(ag_policy.deny("search_web"))
            policies.append(ag_policy.deny("read_url_content"))
        elif eff.internet_access_policy == AccessPolicy.ASK:
            policies.append(ag_policy.ask_user("search_web", handler=approval_handler))
            policies.append(ag_policy.ask_user("read_url_content", handler=approval_handler))

        return policies

    def _build_prompt_payload(self, request: AgentTurnRequest) -> Any:
        if not request.attachments or types is None:
            return request.prompt
        parts: list[Any] = [request.prompt]
        for att in request.attachments:
            if not att.local_path:
                continue
            p = Path(att.local_path)
            if not p.exists():
                continue
            raw = p.read_bytes()
            if att.mime_type.startswith("image/"):
                parts.append(types.Image(data=raw, mime_type=att.mime_type, description=att.filename))
            else:
                parts.append(types.Document(data=raw, mime_type=att.mime_type, description=att.filename))
        return parts

    async def run_turn(
        self,
        request: AgentTurnRequest,
        callbacks: AgentTurnCallbacks,
    ) -> AgentTurnResult:
        if not SDK_IMPORTABLE or Agent is None or LocalAgentConfig is None or CapabilitiesConfig is None or types is None:
            raise RuntimeError("google-antigravity SDK is not installed.")

        behavior_map = {
            AgentBehaviorMode.AUTONOMOUS: ag.AgentBehavior.AUTONOMOUS,
            AgentBehaviorMode.INTERACTIVE: ag.AgentBehavior.INTERACTIVE,
            AgentBehaviorMode.MINIMAL: ag.AgentBehavior.MINIMAL,
        }
        capabilities = CapabilitiesConfig(
            enable_subagents=request.settings.enable_subagents,
            agent_behavior=behavior_map.get(request.settings.agent_behavior, ag.AgentBehavior.AUTONOMOUS),
            run_command_config=types.RunCommandConfig(
                enable_sandbox=request.settings.terminal_sandbox
                or request.settings.execution_policy == ExecutionPolicy.PROCEED_IN_SANDBOX,
            ),
        )

        # Ensure conversation_id satisfies SDK constraint (>=32 chars, alnum + hyphens)
        safe_conv_id = "".join(c if c.isalnum() or c == "-" else "-" for c in request.conversation_id)
        if len(safe_conv_id) < 32:
            safe_conv_id = (safe_conv_id + "-" + "0" * 32)[:36]

        use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true", "yes")
        config = LocalAgentConfig(
            system_instructions=request.system_instructions,
            capabilities=capabilities,
            policies=self._build_policies(request),
            mcp_servers=self._build_mcp_servers(),
            workspaces=[request.workspace_path] if request.workspace_path else None,
            conversation_id=safe_conv_id,
            save_dir=str(self.store.sessions_dir),
            skills_paths=request.skills_paths or None,
            model=request.settings.model,
            api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"),
            vertex=True if use_vertex else None,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT") if use_vertex else None,
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1") if use_vertex else None,
        )

        text_chunks: list[str] = []
        thought_chunks: list[str] = []
        tool_records: list[ToolCallRecord] = []

        async with Agent(config) as agent:
            response = await agent.chat(self._build_prompt_payload(request))
            self._active_responses[request.conversation_id] = response

            async def pump_tokens() -> None:
                async for tok in response:
                    if tok:
                        text_chunks.append(tok)
                        await callbacks.on_token(tok)

            async def pump_thoughts() -> None:
                try:
                    async for th in response.thoughts:
                        text = str(th)
                        if text:
                            thought_chunks.append(text)
                            await callbacks.on_thought(text)
                except Exception:
                    return

            async def pump_tools() -> None:
                try:
                    async for call in response.tool_calls:
                        name = getattr(call, "name", "tool")
                        raw_args = getattr(call, "args", {}) or {}
                        args = dict(raw_args) if isinstance(raw_args, dict) else {"args": str(raw_args)}
                        rec = ToolCallRecord(
                            name=name,
                            args=args,
                            status=ToolCallStatus.COMPLETED,
                            output=str(getattr(call, "result", "") or "")[:4000] or None,
                            completed_at=utc_now(),
                        )
                        tool_records.append(rec)
                        await callbacks.on_tool_call(rec)
                except Exception:
                    return

            try:
                await asyncio.gather(pump_tokens(), pump_thoughts(), pump_tools())
            finally:
                self._active_responses.pop(request.conversation_id, None)

            usage: UsageMetrics | None = None
            um = getattr(response, "usage_metadata", None)
            if um is not None:
                usage = UsageMetrics(
                    prompt_tokens=int(getattr(um, "prompt_token_count", 0) or 0),
                    completion_tokens=int(getattr(um, "candidates_token_count", 0) or 0),
                    total_tokens=int(getattr(um, "total_token_count", 0) or 0),
                )

            return AgentTurnResult(
                content="".join(text_chunks),
                thoughts="".join(thought_chunks) or None,
                tool_calls=tool_records,
                usage=usage,
                backend_name=self.name,
            )

    async def cancel(self, conversation_id: str) -> bool:
        resp = self._active_responses.get(conversation_id)
        if resp and hasattr(resp, "cancel"):
            try:
                res = resp.cancel()
                if asyncio.iscoroutine(res):
                    await res
                return True
            except Exception:
                return False
        return False
