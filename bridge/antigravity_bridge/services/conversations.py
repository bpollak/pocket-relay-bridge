"""Conversation orchestration, slash command & @mention resolution, and runtime routing."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..config import BridgeStore
from ..models import (
    Attachment,
    Conversation,
    ConversationSummary,
    MentionRef,
    Message,
    MessageRole,
    RuntimeMode,
    StreamEvent,
    SubagentSession,
    SubagentState,
    ToolCallRecord,
    new_id,
    utc_now,
)
from ..runtime.acp_backend import AntigravityACPBackend
from ..runtime.base import AgentBackend, AgentTurnCallbacks, AgentTurnRequest
from ..runtime.local_fallback import LocalFallbackBackend
from ..runtime.sdk_backend import AntigravitySDKBackend
from .customizations import CustomizationsService
from .workspace import WorkspaceService

EventEmitter = Callable[[StreamEvent], Awaitable[None]]

SLASH_INSTRUCTIONS: dict[str, str] = {
    "/plan": "Create a concise, structured step-by-step implementation plan before making changes. Highlight risks and verification steps.",
    "/goal": "Operate in thorough autonomous goal mode: verify each step and do not stop until the goal is fully achieved.",
    "/boost": "Engage deep architectural reasoning, evaluate trade-offs from multiple perspectives, and verify rigor.",
    "/grill-me": "Interview the user with focused clarifying questions to resolve key design ambiguities before building.",
    "/teamwork": "Decompose the task into parallel workstreams and coordinate specialized subagents.",
    "/swarm": "Coordinate parallel worker subagents on distinct slices of the problem and synthesize the outcome.",
    "/compact": "Produce a compact, lossless technical summary of the conversation so context can be reset cleanly.",
    "/browser": "Focus on web browsing, UI verification, or live web interaction.",
    "/learn": "Extract the reusable workflow preference or fix into a crisp rule or skill candidate.",
    "/btw": "Answer this quick side question concisely without disrupting the broader plan.",
}


class ConversationService:
    def __init__(
        self,
        store: BridgeStore,
        workspace_service: WorkspaceService,
        customizations: CustomizationsService,
        sdk_backend: AntigravitySDKBackend,
        acp_backend: AntigravityACPBackend,
        fallback_backend: LocalFallbackBackend,
        emit: EventEmitter,
    ) -> None:
        self.store = store
        self.workspace_service = workspace_service
        self.customizations = customizations
        self.sdk_backend = sdk_backend
        self.acp_backend = acp_backend
        self.fallback_backend = fallback_backend
        self._emit = emit
        self._attachments: dict[str, Attachment] = {}
        self._running_tasks: dict[str, asyncio.Task[Any]] = {}
        self._subagents: dict[str, SubagentSession] = {}

    def select_backend(self, mode: RuntimeMode) -> AgentBackend:
        if mode == RuntimeMode.SDK and self.sdk_backend.is_authenticated():
            return self.sdk_backend
        if mode == RuntimeMode.ACP and self.acp_backend.is_available():
            return self.acp_backend
        if mode == RuntimeMode.LOCAL_FALLBACK:
            return self.fallback_backend
        # AUTO preference: SDK (when authenticated) -> ACP (when `agy` installed) -> Local Fallback
        if self.sdk_backend.is_authenticated():
            return self.sdk_backend
        if self.acp_backend.is_available():
            return self.acp_backend
        return self.fallback_backend

    def active_backend_name(self) -> str:
        return self.select_backend(self.store.settings.runtime_mode).name

    def list_conversations(self, workspace_id: str | None = None) -> list[ConversationSummary]:
        items: list[ConversationSummary] = []
        for conv in self.store.conversations.values():
            if workspace_id and conv.workspace_id != workspace_id:
                continue
            ws_name = (
                self.store.workspaces[conv.workspace_id].name
                if conv.workspace_id and conv.workspace_id in self.store.workspaces
                else None
            )
            items.append(conv.to_summary(workspace_name=ws_name))
        items.sort(key=lambda c: (c.is_pinned, c.updated_at), reverse=True)
        return items

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self.store.conversations.get(conversation_id)

    def create_conversation(
        self,
        title: str | None = None,
        workspace_id: str | None = None,
        model: str | None = None,
    ) -> Conversation:
        if workspace_id is None and self.store.workspaces:
            workspace_id = next(iter(self.store.workspaces.keys()))
        eff = self.store.effective_settings(workspace_id)
        conv = Conversation(
            title=title or "New Conversation",
            workspace_id=workspace_id,
            model=model or eff.model,
        )
        self.store.conversations[conv.id] = conv
        self.store.save()
        return conv

    def delete_conversation(self, conversation_id: str) -> bool:
        task = self._running_tasks.pop(conversation_id, None)
        if task and not task.done():
            task.cancel()
        self.acp_backend.reset_session(conversation_id)
        removed = self.store.conversations.pop(conversation_id, None)
        if removed:
            self.store.save()
            return True
        return False

    def toggle_pin(self, conversation_id: str, pinned: bool) -> Conversation:
        conv = self.store.conversations.get(conversation_id)
        if not conv:
            raise KeyError("Conversation not found")
        conv.is_pinned = pinned
        conv.updated_at = utc_now()
        self.store.save()
        return conv

    def save_upload(self, filename: str, data: bytes, mime_type: str | None = None) -> Attachment:
        guessed = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        att_id = new_id("att")
        safe_name = "".join(ch for ch in Path(filename.replace("\x00", "")).name if ch.isprintable())
        if len(safe_name) > 120:  # keep well under the 255-byte filename limit
            stem, dot, ext = safe_name.rpartition(".")
            ext = (dot + ext) if dot and 0 < len(ext) <= 16 else ""
            safe_name = safe_name[: 120 - len(ext)] + ext
        while len(safe_name.encode("utf-8")) > 200:
            safe_name = safe_name[1:]
        if safe_name in ("", ".", ".."):
            safe_name = "attachment"
        dest = self.store.uploads_dir / f"{att_id}_{safe_name}"
        dest.write_bytes(data)
        preview_b64: str | None = None
        if guessed.startswith("image/") and len(data) <= 300_000:
            preview_b64 = base64.b64encode(data).decode("ascii")
        att = Attachment(
            id=att_id,
            filename=safe_name,
            mime_type=guessed,
            size_bytes=len(data),
            local_path=str(dest),
            preview_base64=preview_b64,
        )
        self._attachments[att.id] = att
        return att

    def list_subagents(self, conversation_id: str | None = None) -> list[SubagentSession]:
        items = list(self._subagents.values())
        if conversation_id:
            items = [s for s in items if s.conversation_id == conversation_id]
        items.sort(key=lambda s: s.updated_at, reverse=True)
        return items

    async def kill_subagent(self, subagent_id: str) -> bool:
        sub = self._subagents.get(subagent_id)
        if not sub:
            return False
        sub.state = SubagentState.KILLED
        sub.updated_at = utc_now()
        await self._emit(
            StreamEvent(
                type="subagent_update",
                conversation_id=sub.conversation_id,
                payload=sub.model_dump(mode="json"),
            )
        )
        return True

    def _resolve_mentions_context(self, conv: Conversation, mentions: list[MentionRef]) -> str:
        if not mentions:
            return ""
        blocks: list[str] = []
        for m in mentions:
            if m.kind == "file" and conv.workspace_id:
                try:
                    content = self.workspace_service.read_file_content(conv.workspace_id, m.id, max_bytes=16_000)
                    blocks.append(f"### Mentioned File: `{m.id}`\n```\n{content}\n```")
                except Exception:
                    blocks.append(f"### Mentioned File: `{m.id}` (unavailable)")
            elif m.kind == "folder" and conv.workspace_id:
                try:
                    entries = self.workspace_service.list_files(conv.workspace_id, m.id)
                    listing = "\n".join(f"- {e.relative_path}{'/' if e.is_directory else ''}" for e in entries[:40])
                    blocks.append(f"### Mentioned Folder: `{m.id}`\n{listing}")
                except Exception:
                    blocks.append(f"### Mentioned Folder: `{m.id}`")
            elif m.kind == "conversation":
                other = self.store.conversations.get(m.id)
                if other:
                    snippet = "\n".join(f"{msg.role.value}: {msg.content[:400]}" for msg in other.messages[-6:])
                    blocks.append(f"### Referenced Conversation: `{other.title}`\n{snippet}")
            elif m.kind == "rule":
                blocks.append(f"### Active Rule Reference: `{m.label}` ({m.detail or m.id})")
            elif m.kind == "mcp":
                blocks.append(f"### Preferred MCP Server: `{m.label}`")
        return "\n\n".join(blocks)

    async def send_message(
        self,
        conversation_id: str,
        content: str,
        mentions: list[MentionRef] | None = None,
        attachment_ids: list[str] | None = None,
        slash_command: str | None = None,
        model: str | None = None,
    ) -> Message:
        conv = self.store.conversations.get(conversation_id)
        if not conv:
            raise KeyError("Conversation not found")

        mentions = mentions or []
        attachments = [self._attachments[aid] for aid in (attachment_ids or []) if aid in self._attachments]

        # Auto-extract leading slash command if present in message text
        effective_slash = slash_command
        stripped = content.strip()
        if not effective_slash and stripped.startswith("/"):
            first = stripped.split()[0].lower()
            if first in SLASH_INSTRUCTIONS:
                effective_slash = first

        if model:
            conv.model = model

        user_msg = Message(
            role=MessageRole.USER,
            content=content,
            attachments=attachments,
            mentions=mentions,
            slash_command=effective_slash,
            model=conv.model,
        )
        conv.messages.append(user_msg)
        if conv.title == "New Conversation" and stripped:
            clean_title = stripped.lstrip("/").strip()
            conv.title = clean_title[:56] + ("…" if len(clean_title) > 56 else "")
        conv.is_streaming = True
        conv.updated_at = utc_now()
        self.store.save()

        await self._emit(
            StreamEvent(
                type="message_created",
                conversation_id=conv.id,
                payload=user_msg.model_dump(mode="json"),
            )
        )

        # Run assistant turn asynchronously so HTTP returns immediately while WebSocket streams
        task = asyncio.create_task(
            self._execute_turn(conv, user_msg, effective_slash),
            name=f"turn-{conv.id}",
        )
        self._running_tasks[conv.id] = task
        return user_msg

    async def _execute_turn(
        self,
        conv: Conversation,
        user_msg: Message,
        slash_command: str | None,
    ) -> None:
        assistant_msg = Message(
            role=MessageRole.ASSISTANT,
            content="",
            thoughts="",
            model=conv.model,
        )
        conv.messages.append(assistant_msg)
        await self._emit(
            StreamEvent(
                type="turn_started",
                conversation_id=conv.id,
                payload={"message_id": assistant_msg.id, "model": conv.model},
            )
        )

        ws_path = (
            self.store.workspaces[conv.workspace_id].path
            if conv.workspace_id and conv.workspace_id in self.store.workspaces
            else None
        )
        eff = self.store.effective_settings(conv.workspace_id)
        eff.model = conv.model
        backend = self.select_backend(eff.runtime_mode)

        # Track subagents when /teamwork or /swarm is invoked
        spawned_subagent: SubagentSession | None = None
        if slash_command in ("/teamwork", "/swarm"):
            spawned_subagent = SubagentSession(
                conversation_id=conv.id,
                name="coordinator",
                role="Swarm Coordinator" if slash_command == "/swarm" else "Teamwork Lead",
                model=conv.model,
                state=SubagentState.WORKING,
                latest_message="Orchestrating subtasks...",
            )
            self._subagents[spawned_subagent.id] = spawned_subagent
            await self._emit(
                StreamEvent(
                    type="subagent_update",
                    conversation_id=conv.id,
                    payload=spawned_subagent.model_dump(mode="json"),
                )
            )

        mention_ctx = self._resolve_mentions_context(conv, user_msg.mentions)
        composed_prompt = user_msg.content
        if mention_ctx:
            composed_prompt = f"{mention_ctx}\n\n---\n\n{user_msg.content}"

        sys_parts: list[str] = []
        if slash_command and slash_command in SLASH_INSTRUCTIONS:
            sys_parts.append(f"Active slash command ({slash_command}): {SLASH_INSTRUCTIONS[slash_command]}")

        skills_paths = [str(p) for _, p in self.customizations.skill_directories(Path(ws_path) if ws_path else None)]

        async def on_token(delta: str) -> None:
            assistant_msg.content += delta
            await self._emit(
                StreamEvent(
                    type="token",
                    conversation_id=conv.id,
                    payload={"message_id": assistant_msg.id, "delta": delta},
                )
            )

        async def on_thought(delta: str) -> None:
            assistant_msg.thoughts = (assistant_msg.thoughts or "") + delta
            await self._emit(
                StreamEvent(
                    type="thought",
                    conversation_id=conv.id,
                    payload={"message_id": assistant_msg.id, "delta": delta},
                )
            )

        async def on_tool_call(rec: ToolCallRecord) -> None:
            assistant_msg.tool_calls.append(rec)
            await self._emit(
                StreamEvent(
                    type="tool_call",
                    conversation_id=conv.id,
                    payload={"message_id": assistant_msg.id, "tool_call": rec.model_dump(mode="json")},
                )
            )

        async def on_tool_update(rec: ToolCallRecord) -> None:
            for idx, existing in enumerate(assistant_msg.tool_calls):
                if existing.id == rec.id:
                    assistant_msg.tool_calls[idx] = rec
                    break
            await self._emit(
                StreamEvent(
                    type="tool_update",
                    conversation_id=conv.id,
                    payload={"message_id": assistant_msg.id, "tool_call": rec.model_dump(mode="json")},
                )
            )

        try:
            turn_req = AgentTurnRequest(
                conversation_id=conv.id,
                prompt=composed_prompt,
                workspace_path=ws_path,
                settings=eff,
                attachments=user_msg.attachments,
                skills_paths=skills_paths,
                system_instructions="\n\n".join(sys_parts) if sys_parts else None,
                slash_command=slash_command,
            )
            result = await backend.run_turn(
                turn_req,
                AgentTurnCallbacks(
                    on_token=on_token,
                    on_thought=on_thought,
                    on_tool_call=on_tool_call,
                    on_tool_update=on_tool_update,
                ),
            )
            if not assistant_msg.content and result.content:
                assistant_msg.content = result.content
            if not assistant_msg.thoughts and result.thoughts:
                assistant_msg.thoughts = result.thoughts
            if not assistant_msg.tool_calls and result.tool_calls:
                assistant_msg.tool_calls = result.tool_calls
            assistant_msg.usage = result.usage

            if spawned_subagent:
                spawned_subagent.state = SubagentState.COMPLETED
                spawned_subagent.latest_message = "Completed turn synthesis."
                spawned_subagent.updated_at = utc_now()
                await self._emit(
                    StreamEvent(
                        type="subagent_update",
                        conversation_id=conv.id,
                        payload=spawned_subagent.model_dump(mode="json"),
                    )
                )

            await self._emit(
                StreamEvent(
                    type="turn_completed",
                    conversation_id=conv.id,
                    payload={
                        "message": assistant_msg.model_dump(mode="json"),
                        "backend": result.backend_name,
                    },
                )
            )
        except asyncio.CancelledError:
            assistant_msg.content += "\n\n_(Generation stopped)_"
            await self._emit(
                StreamEvent(
                    type="turn_cancelled",
                    conversation_id=conv.id,
                    payload={"message_id": assistant_msg.id},
                )
            )
        except Exception as exc:
            assistant_msg.content = f"**Antigravity runtime error:** `{exc}`"
            await self._emit(
                StreamEvent(
                    type="turn_error",
                    conversation_id=conv.id,
                    payload={"message_id": assistant_msg.id, "error": str(exc)},
                )
            )
        finally:
            conv.is_streaming = False
            conv.updated_at = utc_now()
            self._running_tasks.pop(conv.id, None)
            self.store.save()

    async def cancel_turn(self, conversation_id: str) -> bool:
        await self.sdk_backend.cancel(conversation_id)
        await self.acp_backend.cancel(conversation_id)
        await self.fallback_backend.cancel(conversation_id)
        task = self._running_tasks.get(conversation_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    async def compact_conversation(self, conversation_id: str) -> Conversation:
        conv = self.store.conversations.get(conversation_id)
        if not conv:
            raise KeyError("Conversation not found")
        if len(conv.messages) <= 4:
            return conv
        older = conv.messages[:-2]
        summary_lines = [
            f"- **{m.role.value}**: {m.content.strip().splitlines()[0][:140]}"
            for m in older
            if m.content.strip()
        ]
        summary_msg = Message(
            role=MessageRole.SYSTEM,
            content="### Compacted Conversation Context\n" + "\n".join(summary_lines[:15]),
        )
        conv.messages = [summary_msg, *conv.messages[-2:]]
        conv.updated_at = utc_now()
        self.acp_backend.reset_session(conversation_id)
        self.store.save()
        return conv
