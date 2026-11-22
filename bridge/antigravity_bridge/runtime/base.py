"""Abstract runtime protocol and turn structures."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..models import (
    Attachment,
    EffectiveSettings,
    ToolCallRecord,
    UsageMetrics,
)


@dataclass
class AgentTurnRequest:
    conversation_id: str
    prompt: str
    workspace_path: str | None
    settings: EffectiveSettings
    attachments: list[Attachment] = field(default_factory=list)
    skills_paths: list[str] = field(default_factory=list)
    system_instructions: str | None = None
    slash_command: str | None = None


@dataclass
class AgentTurnCallbacks:
    on_token: Callable[[str], Awaitable[None]]
    on_thought: Callable[[str], Awaitable[None]]
    on_tool_call: Callable[[ToolCallRecord], Awaitable[None]]
    on_tool_update: Callable[[ToolCallRecord], Awaitable[None]]
    on_subagent_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None


@dataclass
class AgentTurnResult:
    content: str
    thoughts: str | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    usage: UsageMetrics | None = None
    backend_name: str = "unknown"


class AgentBackend(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def is_authenticated(self) -> bool: ...

    async def run_turn(
        self,
        request: AgentTurnRequest,
        callbacks: AgentTurnCallbacks,
    ) -> AgentTurnResult: ...

    async def cancel(self, conversation_id: str) -> bool: ...
