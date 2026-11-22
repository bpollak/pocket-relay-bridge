"""Discovery and management of Antigravity Skills, Rules, MCP Servers, Plugins, and Slash Commands."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..config import BridgeStore
from ..models import (
    CustomizationsSnapshot,
    MCPServerItem,
    PluginItem,
    RuleItem,
    SkillItem,
    SlashCommandInfo,
)

BUILTIN_SLASH_COMMANDS: list[SlashCommandInfo] = [
    SlashCommandInfo(name="/plan", title="Implementation Plan", description="Design a step-by-step plan and review before executing.", category="planning"),
    SlashCommandInfo(name="/goal", title="Autonomous Goal", description="Run an autonomous multi-step loop until the goal is verified complete.", category="execution"),
    SlashCommandInfo(name="/boost", title="Boost Deep Reasoning", description="Deep architectural thinking, multiple perspectives, and rigorous verification.", category="reasoning"),
    SlashCommandInfo(name="/grill-me", title="Grill Me", description="Interactive interview to resolve design ambiguities before building.", category="planning"),
    SlashCommandInfo(name="/teamwork", title="Agent Teamwork", description="Coordinate multiple specialized agents working in parallel.", category="multi-agent"),
    SlashCommandInfo(name="/swarm", title="Swarm Mode", description="Launch a coordinated swarm of parallel workers on a shared board.", category="multi-agent"),
    SlashCommandInfo(name="/compact", title="Compact Context", description="Summarize conversation history to free up context window.", category="context"),
    SlashCommandInfo(name="/schedule", title="Schedule Task", description="Create a recurring cron job or one-time delayed timer.", category="automation"),
    SlashCommandInfo(name="/browser", title="Browser Automation", description="Navigate, inspect, or test a live web page or web app.", category="tools"),
    SlashCommandInfo(name="/run-workflow", title="Dynamic Workflow", description="Orchestrate a parallel or staged Python agent workflow.", category="multi-agent"),
    SlashCommandInfo(name="/learn", title="Persist Learning", description="Capture a correction or workflow pattern into persistent memory/skills.", category="memory"),
    SlashCommandInfo(name="/btw", title="Side Question", description="Ask a quick side question without derailing the active task.", category="context"),
]


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, flags=re.DOTALL)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    lines = match.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        value = v.strip()
        indicator = _BLOCK_SCALAR_RE.match(value)
        if indicator and not line[:1].isspace():
            # YAML block scalar (`>`, `>-`, `|`, `|-`, `>+`, `|+`): the value is the
            # following indented (or blank) lines.
            block: list[str] = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1].isspace()):
                block.append(lines[i])
                i += 1
            meta[k.strip().lower()] = _fold_block_scalar(block, literal=indicator.group(1) == "|")
            continue
        meta[k.strip().lower()] = value.strip('"').strip("'")
    return meta, text[match.end() :]


_BLOCK_SCALAR_RE = re.compile(r"^([>|])[+-]?\d*[+-]?\s*(?:#.*)?$")


def _fold_block_scalar(block: list[str], literal: bool) -> str:
    indents = [len(ln) - len(ln.lstrip()) for ln in block if ln.strip()]
    indent = min(indents) if indents else 0
    dedented = [ln[indent:].rstrip() if ln.strip() else "" for ln in block]
    if literal:
        return "\n".join(dedented).strip()
    # Folded: single newlines become spaces, blank lines become a newline.
    paragraphs: list[str] = []
    current: list[str] = []
    for ln in dedented:
        if ln:
            current.append(ln.strip())
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n".join(paragraphs).strip()


class CustomizationsService:
    def __init__(self, store: BridgeStore) -> None:
        self.store = store

    def snapshot(self, workspace_id: str | None = None, include_content: bool = False) -> CustomizationsSnapshot:
        ws_path = (
            Path(self.store.workspaces[workspace_id].path)
            if workspace_id and workspace_id in self.store.workspaces
            else None
        )
        return CustomizationsSnapshot(
            skills=self.discover_skills(ws_path, include_content=include_content),
            rules=self.discover_rules(ws_path, include_content=include_content),
            mcp_servers=self.discover_mcp_servers(),
            plugins=self.discover_plugins(),
            slash_commands=list(BUILTIN_SLASH_COMMANDS),
        )

    def skill_directories(self, workspace_path: Path | None = None) -> list[tuple[str, Path]]:
        home = Path.home()
        roots: list[tuple[str, Path]] = [
            ("builtin", home / ".gemini" / "antigravity" / "builtin" / "skills"),
            ("user", home / ".gemini" / "antigravity" / "skills"),
            ("user", home / ".agents" / "skills"),
            ("user", home / ".agents" / "ucsd" / "skills"),
            ("harness", home / ".tritonai-harness" / "codex" / "skills"),
        ]
        if workspace_path:
            roots.insert(0, ("workspace", workspace_path / ".agents" / "skills"))
            roots.insert(1, ("workspace", workspace_path / ".agent" / "skills"))
        return [(src, p) for src, p in roots if p.exists() and p.is_dir()]

    def discover_skills(self, workspace_path: Path | None = None, include_content: bool = False) -> list[SkillItem]:
        disabled = set(self.store.settings.disabled_skills)
        seen: set[str] = set()
        skills: list[SkillItem] = []

        for source, root in self.skill_directories(workspace_path):
            for child in sorted(root.iterdir()):
                skill_md = child / "SKILL.md"
                if not child.is_dir() or not skill_md.exists():
                    continue
                try:
                    raw = skill_md.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                meta, body = _parse_frontmatter(raw)
                name = meta.get("name") or child.name
                if name in seen:
                    continue
                seen.add(name)
                desc = meta.get("description") or next(
                    (ln.lstrip("# ").strip() for ln in body.splitlines() if ln.strip()),
                    "Antigravity skill",
                )
                skills.append(
                    SkillItem(
                        id=name,
                        name=name,
                        description=desc[:260],
                        path=str(skill_md),
                        source=source,
                        enabled=(name not in disabled),
                        content=raw[:20_000] if include_content else None,
                    )
                )
        return skills

    def get_skill(self, skill_id: str, workspace_id: str | None = None) -> SkillItem | None:
        ws_path = (
            Path(self.store.workspaces[workspace_id].path)
            if workspace_id and workspace_id in self.store.workspaces
            else None
        )
        for skill in self.discover_skills(ws_path, include_content=True):
            if skill.id == skill_id or skill.name == skill_id:
                return skill
        return None

    def discover_rules(self, workspace_path: Path | None = None, include_content: bool = False) -> list[RuleItem]:
        disabled = set(self.store.settings.disabled_rules)
        home = Path.home()
        candidates: list[tuple[str, Path]] = [
            ("global", home / ".gemini" / "GEMINI.md"),
            ("global", home / ".gemini" / "antigravity" / "rules"),
            ("global", home / ".agents" / "rules"),
        ]
        if workspace_path:
            candidates.append(("workspace", workspace_path / ".agents" / "rules"))
            candidates.append(("workspace", workspace_path / "GEMINI.md"))
            candidates.append(("workspace", workspace_path / "AGENTS.md"))

        rules: list[RuleItem] = []
        seen: set[str] = set()
        for scope, target in candidates:
            if not target.exists():
                continue
            files = sorted(target.glob("*.md")) if target.is_dir() else [target]
            for fp in files:
                key = str(fp.resolve())
                if key in seen:
                    continue
                seen.add(key)
                try:
                    raw = fp.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                rule_id = f"{scope}:{fp.stem}"
                preview = next((ln.lstrip("# ").strip() for ln in raw.splitlines() if ln.strip()), fp.name)[:180]
                rules.append(
                    RuleItem(
                        id=rule_id,
                        name=fp.name,
                        scope=scope,
                        path=str(fp),
                        enabled=(rule_id not in disabled),
                        preview=preview,
                        content=raw[:20_000] if include_content else None,
                    )
                )
        return rules

    def discover_mcp_servers(self) -> list[MCPServerItem]:
        disabled = set(self.store.settings.disabled_mcp_servers)
        cfg_paths = [
            Path.home() / ".gemini" / "antigravity" / "mcp_config.json",
            Path.home() / ".gemini" / "config" / "mcp_config.json",
        ]
        servers: list[MCPServerItem] = []
        seen: set[str] = set()
        for path in cfg_paths:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            mcp_map = data.get("mcpServers") or data.get("mcp_servers") or {}
            if not isinstance(mcp_map, dict):
                continue
            for name, entry in mcp_map.items():
                if name in seen or not isinstance(entry, dict):
                    continue
                seen.add(name)
                url = entry.get("serverUrl") or entry.get("url")
                cmd = entry.get("command")
                transport = "http" if url else "stdio"
                if url and "sse" in str(url).lower():
                    transport = "sse"
                servers.append(
                    MCPServerItem(
                        id=name,
                        name=name,
                        transport=transport,
                        command_or_url=str(url or cmd or ""),
                        args=[str(a) for a in (entry.get("args") or [])],
                        enabled=(name not in disabled and not entry.get("disabled", False)),
                        status="disabled" if (name in disabled or entry.get("disabled")) else "configured",
                    )
                )
        return servers

    def discover_plugins(self) -> list[PluginItem]:
        plugins: list[PluginItem] = []
        roots = [
            Path.home() / ".gemini" / "antigravity" / "plugins",
            Path.home() / ".gemini" / "antigravity" / "plugin_data",
        ]
        seen: set[str] = set()
        for root in roots:
            if not root.exists() or not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name in seen or child.name.startswith("."):
                    continue
                seen.add(child.name)
                manifest = child / "plugin.json"
                desc = "Installed Antigravity plugin"
                version = "1.0.0"
                if manifest.exists():
                    try:
                        m = json.loads(manifest.read_text(encoding="utf-8"))
                        desc = m.get("description") or desc
                        version = m.get("version") or version
                    except Exception:
                        pass
                plugins.append(
                    PluginItem(
                        id=child.name,
                        name=child.name,
                        version=version,
                        description=desc,
                        source="local",
                        enabled=True,
                    )
                )
        return plugins

    def toggle(self, kind: str, item_id: str, enabled: bool) -> None:
        if kind == "skill":
            target_list = self.store.settings.disabled_skills
        elif kind == "rule":
            target_list = self.store.settings.disabled_rules
        elif kind == "mcp":
            target_list = self.store.settings.disabled_mcp_servers
        else:
            raise ValueError(f"Unsupported customization kind: {kind}")

        if enabled:
            if item_id in target_list:
                target_list.remove(item_id)
        else:
            if item_id not in target_list:
                target_list.append(item_id)
        self.store.save()
