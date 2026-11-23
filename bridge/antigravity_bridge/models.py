"""Pydantic v2 data models shared across REST and WebSocket endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    raw = uuid4().hex
    return f"{prefix}-{raw}" if prefix else raw


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ExecutionPolicy(str, Enum):
    ALWAYS_PROCEED = "always-proceed"
    REQUEST_REVIEW = "request-review"
    STRICT = "strict"
    PROCEED_IN_SANDBOX = "proceed-in-sandbox"


class AccessPolicy(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class RuntimeMode(str, Enum):
    AUTO = "auto"
    SDK = "sdk"
    ACP = "acp"
    LOCAL_FALLBACK = "local-fallback"


class AgentBehaviorMode(str, Enum):
    AUTONOMOUS = "autonomous"
    INTERACTIVE = "interactive"
    MINIMAL = "minimal"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ToolCallStatus(str, Enum):
    PENDING_APPROVAL = "pending_approval"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"


class ApprovalDecision(str, Enum):
    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    DENY = "deny"


class TaskScheduleKind(str, Enum):
    CRON = "cron"
    TIMER = "timer"


class SubagentState(str, Enum):
    WORKING = "working"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


# ---------------------------------------------------------------------------
# Settings & Permissions
# ---------------------------------------------------------------------------


class PermissionGrant(BaseModel):
    id: str = Field(default_factory=lambda: new_id("grant"))
    kind: str  # "file" | "command" | "url" | "tool"
    pattern: str
    effect: str = "allow"  # "allow" | "deny"
    scope: str = "global"  # "global" | workspace_id
    created_at: datetime = Field(default_factory=utc_now)


class ProjectSettingsOverride(BaseModel):
    file_access_policy: AccessPolicy | None = None
    internet_access_policy: AccessPolicy | None = None
    sandbox_mode: bool | None = None
    execution_policy: ExecutionPolicy | None = None
    model: str | None = None
    permission_grants: list[PermissionGrant] = Field(default_factory=list)


class GlobalSettings(BaseModel):
    model: str = "gemini-2.5-pro"
    runtime_mode: RuntimeMode = RuntimeMode.AUTO
    agent_behavior: AgentBehaviorMode = AgentBehaviorMode.AUTONOMOUS
    execution_policy: ExecutionPolicy = ExecutionPolicy.REQUEST_REVIEW
    terminal_sandbox: bool = False
    non_workspace_file_access: AccessPolicy = AccessPolicy.ASK
    internet_access_policy: AccessPolicy = AccessPolicy.ALLOW
    command_allowlist: list[str] = Field(
        default_factory=lambda: ["git status", "git diff", "ls", "pwd", "cat", "pytest"]
    )
    command_denylist: list[str] = Field(
        default_factory=lambda: ["rm -rf /", "sudo rm -rf", "mkfs"]
    )
    browser_allowlist: list[str] = Field(default_factory=list)
    permission_grants: list[PermissionGrant] = Field(default_factory=list)
    enable_subagents: bool = True
    notifications_enabled: bool = True
    keep_awake: bool = True
    appearance: str = "system"  # "system" | "dark" | "light"
    disabled_skills: list[str] = Field(default_factory=list)
    disabled_mcp_servers: list[str] = Field(default_factory=list)
    disabled_rules: list[str] = Field(default_factory=list)


class EffectiveSettings(BaseModel):
    model: str
    runtime_mode: RuntimeMode
    agent_behavior: AgentBehaviorMode
    execution_policy: ExecutionPolicy
    terminal_sandbox: bool
    non_workspace_file_access: AccessPolicy
    internet_access_policy: AccessPolicy
    command_allowlist: list[str]
    command_denylist: list[str]
    browser_allowlist: list[str]
    enable_subagents: bool


# ---------------------------------------------------------------------------
# Host & Pairing
# ---------------------------------------------------------------------------


class HostStatus(BaseModel):
    host_id: str
    host_name: str
    version: str = "1.0.0"
    platform: str = "macOS"
    tailscale_dns: str | None = None
    tailscale_ip: str | None = None
    active_backend: str
    sdk_available: bool
    sdk_authenticated: bool
    acp_available: bool
    available_models: list[str]
    active_conversations: int
    running_tasks: int
    pending_approvals: int
    uptime_seconds: float


class PairingInfo(BaseModel):
    host_id: str
    host_name: str
    base_url: str
    tailscale_url: str | None = None
    local_url: str
    token: str
    pairing_uri: str
    #: Remote access (set when it is turned on): relay address, this bridge's room and tunnel key.
    relay_url: str | None = None
    relay_room: str | None = None
    relay_key: str | None = None


class PairCodeExchangeRequest(BaseModel):
    code: str


class PushDeviceRequest(BaseModel):
    device_token: str
    environment: str = "production"
    show_detail: bool = False


# ---------------------------------------------------------------------------
# Workspaces / Projects & Files & Diffs
# ---------------------------------------------------------------------------


class Workspace(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ws"))
    name: str
    path: str
    git_branch: str | None = None
    is_git_repo: bool = False
    last_opened_at: datetime = Field(default_factory=utc_now)
    settings_override: ProjectSettingsOverride = Field(default_factory=ProjectSettingsOverride)


class CreateWorkspaceRequest(BaseModel):
    path: str
    name: str | None = None


class FileEntry(BaseModel):
    name: str
    path: str
    relative_path: str
    is_directory: bool
    size_bytes: int = 0
    modified_at: datetime | None = None


class DiffFile(BaseModel):
    path: str
    status: str  # "M", "A", "D", "R", "??"
    additions: int = 0
    deletions: int = 0
    patch: str = ""
    staged: bool = False


class GitDiffSummary(BaseModel):
    workspace_id: str
    branch: str | None = None
    files: list[DiffFile] = Field(default_factory=list)
    total_additions: int = 0
    total_deletions: int = 0


# ---------------------------------------------------------------------------
# Mentions, Slash Commands, Attachments, Messages, Conversations
# ---------------------------------------------------------------------------


class Attachment(BaseModel):
    id: str = Field(default_factory=lambda: new_id("att"))
    filename: str
    mime_type: str
    size_bytes: int
    local_path: str | None = None
    preview_base64: str | None = None


class MentionRef(BaseModel):
    kind: str  # "file" | "folder" | "conversation" | "rule" | "mcp" | "terminal"
    id: str
    label: str
    detail: str | None = None


class SlashCommandInfo(BaseModel):
    name: str  # e.g. "/plan"
    title: str
    description: str
    category: str = "workflow"


class ToolCallRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tool"))
    name: str
    # What sort of step this is, when the agent says (ACP: read, edit, delete, move, search,
    # execute, think, fetch, other). Lets the phone show the activity without the detail.
    kind: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    status: ToolCallStatus = ToolCallStatus.RUNNING
    output: str | None = None
    error: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class UsageMetrics(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Message(BaseModel):
    id: str = Field(default_factory=lambda: new_id("msg"))
    role: MessageRole
    content: str
    thoughts: str | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    mentions: list[MentionRef] = Field(default_factory=list)
    slash_command: str | None = None
    model: str | None = None
    usage: UsageMetrics | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ConversationSummary(BaseModel):
    id: str
    title: str
    workspace_id: str | None = None
    workspace_name: str | None = None
    model: str
    message_count: int = 0
    last_message_preview: str = ""
    is_streaming: bool = False
    is_pinned: bool = False
    created_at: datetime
    updated_at: datetime


class Conversation(BaseModel):
    id: str = Field(default_factory=lambda: f"agy-{uuid4().hex}")
    title: str = "New Conversation"
    workspace_id: str | None = None
    model: str = "gemini-2.5-pro"
    messages: list[Message] = Field(default_factory=list)
    is_streaming: bool = False
    is_pinned: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def to_summary(self, workspace_name: str | None = None) -> ConversationSummary:
        preview = ""
        if self.messages:
            preview = self.messages[-1].content.strip().replace("\n", " ")[:140]
        return ConversationSummary(
            id=self.id,
            title=self.title,
            workspace_id=self.workspace_id,
            workspace_name=workspace_name,
            model=self.model,
            message_count=len(self.messages),
            last_message_preview=preview,
            is_streaming=self.is_streaming,
            is_pinned=self.is_pinned,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class CreateConversationRequest(BaseModel):
    title: str | None = None
    workspace_id: str | None = None
    model: str | None = None


class SendMessageRequest(BaseModel):
    content: str
    mentions: list[MentionRef] = Field(default_factory=list)
    attachment_ids: list[str] = Field(default_factory=list)
    slash_command: str | None = None
    model: str | None = None


# ---------------------------------------------------------------------------
# Approvals & Interactive Questions
# ---------------------------------------------------------------------------


class PendingApproval(BaseModel):
    id: str = Field(default_factory=lambda: new_id("apr"))
    conversation_id: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str = "Action requires user approval under active policy."
    risk_level: str = "medium"  # "low" | "medium" | "high"
    created_at: datetime = Field(default_factory=utc_now)


class ApprovalResponseRequest(BaseModel):
    decision: ApprovalDecision


class QuestionItem(BaseModel):
    question: str
    options: list[str] = Field(default_factory=list)
    is_multi_select: bool = False


class PendingQuestion(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ask"))
    conversation_id: str
    questions: list[QuestionItem]
    created_at: datetime = Field(default_factory=utc_now)


class QuestionAnswerRequest(BaseModel):
    answers: dict[str, list[str]]  # question index or text -> selected/typed answers


# ---------------------------------------------------------------------------
# Auxiliary Pane: Artifacts, Subagents, Terminals
# ---------------------------------------------------------------------------


class ArtifactItem(BaseModel):
    id: str
    title: str
    kind: str = "markdown"  # "plan" | "walkthrough" | "markdown" | "html" | "image"
    path: str
    conversation_id: str | None = None
    summary: str = ""
    content: str | None = None
    request_feedback: bool = False
    updated_at: datetime = Field(default_factory=utc_now)


class SubagentSession(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sub"))
    conversation_id: str
    name: str
    role: str
    model: str
    state: SubagentState = SubagentState.WORKING
    latest_message: str = ""
    started_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TerminalSession(BaseModel):
    id: str = Field(default_factory=lambda: new_id("term"))
    workspace_id: str | None = None
    command: str
    cwd: str
    status: str = "running"  # "running" | "exited" | "killed"
    exit_code: int | None = None
    output: str = ""
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None


class StartTerminalRequest(BaseModel):
    command: str
    workspace_id: str | None = None


class TerminalInputRequest(BaseModel):
    text: str


# ---------------------------------------------------------------------------
# Customizations: Skills, Rules, MCP Servers, Plugins
# ---------------------------------------------------------------------------


class SkillItem(BaseModel):
    id: str
    name: str
    description: str
    path: str
    source: str  # "builtin" | "user" | "workspace" | "harness"
    enabled: bool = True
    content: str | None = None


class RuleItem(BaseModel):
    id: str
    name: str
    scope: str  # "global" | "workspace"
    path: str
    enabled: bool = True
    preview: str = ""
    content: str | None = None


class MCPServerItem(BaseModel):
    id: str
    name: str
    transport: str  # "stdio" | "http" | "sse"
    command_or_url: str
    args: list[str] = Field(default_factory=list)
    enabled: bool = True
    status: str = "configured"


class PluginItem(BaseModel):
    id: str
    name: str
    version: str = "1.0.0"
    description: str = ""
    source: str = "local"
    enabled: bool = True


class CustomizationsSnapshot(BaseModel):
    skills: list[SkillItem] = Field(default_factory=list)
    rules: list[RuleItem] = Field(default_factory=list)
    mcp_servers: list[MCPServerItem] = Field(default_factory=list)
    plugins: list[PluginItem] = Field(default_factory=list)
    slash_commands: list[SlashCommandInfo] = Field(default_factory=list)


class ToggleCustomizationRequest(BaseModel):
    kind: str  # "skill" | "rule" | "mcp"
    id: str
    enabled: bool


# ---------------------------------------------------------------------------
# Scheduled Tasks
# ---------------------------------------------------------------------------


class ScheduledTaskRun(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    status: str = "running"  # "running" | "succeeded" | "failed"
    conversation_id: str | None = None
    summary: str = ""


class ScheduledTask(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sched"))
    name: str
    prompt: str
    kind: TaskScheduleKind = TaskScheduleKind.CRON
    cron_expression: str | None = "0 9 * * *"
    delay_seconds: int | None = None
    workspace_id: str | None = None
    model: str | None = None
    enabled: bool = True
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    runs: list[ScheduledTaskRun] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class CreateScheduledTaskRequest(BaseModel):
    name: str
    prompt: str
    kind: TaskScheduleKind = TaskScheduleKind.CRON
    cron_expression: str | None = "0 9 * * *"
    delay_seconds: int | None = None
    workspace_id: str | None = None
    model: str | None = None
    enabled: bool = True


class UpdateScheduledTaskRequest(BaseModel):
    name: str | None = None
    prompt: str | None = None
    cron_expression: str | None = None
    enabled: bool | None = None
    workspace_id: str | None = None
    model: str | None = None


# ---------------------------------------------------------------------------
# Global Search / Command Palette
# ---------------------------------------------------------------------------


class SearchResultItem(BaseModel):
    kind: str  # "conversation" | "workspace" | "file" | "skill" | "command" | "artifact"
    id: str
    title: str
    subtitle: str = ""
    workspace_id: str | None = None


# ---------------------------------------------------------------------------
# WebSocket Event Envelope
# ---------------------------------------------------------------------------


class StreamEvent(BaseModel):
    type: str
    conversation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)
