"""Persistent configuration and state management for Pocket Relay Bridge."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from .models import (
    Conversation,
    EffectiveSettings,
    GlobalSettings,
    ScheduledTask,
    Workspace,
    new_id,
)

DEFAULT_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
    "gemini-2.5-flash-lite",
]


def _find_tailscale_binary() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in (
        "/usr/local/bin/tailscale",
        "/opt/homebrew/bin/tailscale",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def detect_tailscale_info() -> tuple[str | None, str | None]:
    """Return (dns_name, ipv4) from local Tailscale daemon if active."""
    ts_bin = _find_tailscale_binary()
    if not ts_bin:
        return None, None
    try:
        proc = subprocess.run(
            [ts_bin, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=2.5,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None, None
        data = json.loads(proc.stdout)
        self_info = data.get("Self") or {}
        dns = (self_info.get("DNSName") or "").rstrip(".") or None
        ips = self_info.get("TailscaleIPs") or []
        ipv4 = next((ip for ip in ips if "." in ip), None)
        return dns, ipv4
    except Exception:
        return None, None


class BridgeStore:
    """Thread-safe JSON-backed state store under ~/.antigravity-mobile (or custom dir)."""

    def __init__(self, data_dir: Path | None = None) -> None:
        env_dir = os.environ.get("ANTIGRAVITY_MOBILE_DATA_DIR")
        self.data_dir = Path(data_dir or env_dir or (Path.home() / ".antigravity-mobile")).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir = self.data_dir / "uploads"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir = self.data_dir / "sdk_sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"

        self.host_id: str = ""
        self.host_name: str = socket.gethostname().split(".")[0]
        self.auth_token: str = ""
        self.settings: GlobalSettings = GlobalSettings()
        self.workspaces: dict[str, Workspace] = {}
        self.conversations: dict[str, Conversation] = {}
        self.scheduled_tasks: dict[str, ScheduledTask] = {}

        self._load()
        self._ensure_defaults()

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            raw: dict[str, Any] = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return

        self.host_id = raw.get("host_id") or ""
        self.host_name = raw.get("host_name") or self.host_name
        self.auth_token = raw.get("auth_token") or ""

        if "settings" in raw:
            try:
                self.settings = GlobalSettings.model_validate(raw["settings"])
            except Exception:
                self.settings = GlobalSettings()

        for item in raw.get("workspaces", []):
            try:
                ws = Workspace.model_validate(item)
                self.workspaces[ws.id] = ws
            except Exception:
                continue

        for item in raw.get("conversations", []):
            try:
                conv = Conversation.model_validate(item)
                conv.is_streaming = False
                self.conversations[conv.id] = conv
            except Exception:
                continue

        for item in raw.get("scheduled_tasks", []):
            try:
                task = ScheduledTask.model_validate(item)
                self.scheduled_tasks[task.id] = task
            except Exception:
                continue

    def _ensure_defaults(self) -> None:
        changed = False
        if not self.host_id:
            self.host_id = new_id("host")
            changed = True
        env_token = os.environ.get("ANTIGRAVITY_BRIDGE_TOKEN")
        if env_token:
            self.auth_token = env_token
        elif not self.auth_token:
            self.auth_token = f"agy_{secrets.token_urlsafe(24)}"
            changed = True

        # Seed default workspace with current repo or ~/Documents if empty
        if not self.workspaces:
            default_path = Path(
                os.environ.get("ANTIGRAVITY_DEFAULT_WORKSPACE") or os.getcwd()
            ).expanduser().resolve()
            if default_path.exists():
                ws = Workspace(
                    name=default_path.name,
                    path=str(default_path),
                    is_git_repo=(default_path / ".git").exists(),
                )
                self.workspaces[ws.id] = ws
                changed = True

        if changed:
            self.save()

    def save(self) -> None:
        payload = {
            "host_id": self.host_id,
            "host_name": self.host_name,
            "auth_token": self.auth_token,
            "settings": self.settings.model_dump(mode="json"),
            "workspaces": [w.model_dump(mode="json") for w in self.workspaces.values()],
            "conversations": [c.model_dump(mode="json") for c in self.conversations.values()],
            "scheduled_tasks": [t.model_dump(mode="json") for t in self.scheduled_tasks.values()],
        }
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)
        try:
            os.chmod(self.state_file, 0o600)
        except OSError:
            pass

    def rotate_token(self) -> str:
        self.auth_token = f"agy_{secrets.token_urlsafe(24)}"
        self.save()
        return self.auth_token

    def effective_settings(self, workspace_id: str | None = None) -> EffectiveSettings:
        g = self.settings
        ov = self.workspaces[workspace_id].settings_override if workspace_id and workspace_id in self.workspaces else None
        return EffectiveSettings(
            model=(ov.model if ov and ov.model else g.model),
            runtime_mode=g.runtime_mode,
            agent_behavior=g.agent_behavior,
            execution_policy=(ov.execution_policy if ov and ov.execution_policy else g.execution_policy),
            terminal_sandbox=(ov.sandbox_mode if ov and ov.sandbox_mode is not None else g.terminal_sandbox),
            non_workspace_file_access=(
                ov.file_access_policy if ov and ov.file_access_policy else g.non_workspace_file_access
            ),
            internet_access_policy=(
                ov.internet_access_policy if ov and ov.internet_access_policy else g.internet_access_policy
            ),
            command_allowlist=list(g.command_allowlist),
            command_denylist=list(g.command_denylist),
            browser_allowlist=list(g.browser_allowlist),
            enable_subagents=g.enable_subagents,
        )
