"""Workspace, file explorer, git diff, and artifact discovery service."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..config import BridgeStore
from ..models import (
    ArtifactItem,
    DiffFile,
    FileEntry,
    GitDiffSummary,
    ProjectSettingsOverride,
    Workspace,
    utc_now,
)

IGNORED_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".build",
    "DerivedData",
    "Pods",
    ".next",
    "dist",
    "build",
    ".pytest_cache",
}


class WorkspaceService:
    def __init__(self, store: BridgeStore) -> None:
        self.store = store

    def list_workspaces(self) -> list[Workspace]:
        items: list[Workspace] = []
        for ws in list(self.store.workspaces.values()):
            self._refresh_git_meta(ws)
            items.append(ws)
        items.sort(key=lambda w: w.last_opened_at, reverse=True)
        return items

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        ws = self.store.workspaces.get(workspace_id)
        if ws:
            self._refresh_git_meta(ws)
        return ws

    def add_workspace(self, path_str: str, name: str | None = None) -> Workspace:
        if not path_str or not path_str.strip() or "\x00" in path_str:
            raise ValueError("Path is required.")
        resolved = Path(path_str.strip()).expanduser().resolve()
        if not resolved.exists() or not resolved.is_dir():
            raise ValueError(f"Directory does not exist: {resolved}")

        for existing in self.store.workspaces.values():
            if Path(existing.path).resolve() == resolved:
                existing.last_opened_at = utc_now()
                self._refresh_git_meta(existing)
                self.store.save()
                return existing

        ws = Workspace(
            name=name or resolved.name,
            path=str(resolved),
            is_git_repo=(resolved / ".git").exists(),
        )
        self._refresh_git_meta(ws)
        self.store.workspaces[ws.id] = ws
        self.store.save()
        return ws

    def remove_workspace(self, workspace_id: str) -> bool:
        removed = self.store.workspaces.pop(workspace_id, None)
        if removed:
            self.store.save()
            return True
        return False

    def update_workspace_settings(
        self, workspace_id: str, override: ProjectSettingsOverride
    ) -> Workspace:
        ws = self.store.workspaces.get(workspace_id)
        if not ws:
            raise KeyError(f"Workspace not found: {workspace_id}")
        ws.settings_override = override
        self.store.save()
        return ws

    def _refresh_git_meta(self, ws: Workspace) -> None:
        root = Path(ws.path)
        ws.is_git_repo = (root / ".git").exists()
        if not ws.is_git_repo:
            ws.git_branch = None
            return
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            ws.git_branch = proc.stdout.strip() if proc.returncode == 0 else None
        except Exception:
            ws.git_branch = None

    def _safe_resolve_inside(self, workspace: Workspace, rel_path: str) -> Path:
        if "\x00" in rel_path:
            raise ValueError("Path contains an invalid character.")
        root = Path(workspace.path).resolve()
        candidate = (root / rel_path).resolve() if rel_path else root
        if candidate != root and root not in candidate.parents:
            raise PermissionError("Path escapes workspace root.")
        return candidate

    def list_files(self, workspace_id: str, rel_path: str = "") -> list[FileEntry]:
        ws = self.store.workspaces.get(workspace_id)
        if not ws:
            raise KeyError("Workspace not found")
        root = Path(ws.path).resolve()
        target = self._safe_resolve_inside(ws, rel_path)
        if not target.exists() or not target.is_dir():
            return []

        entries: list[FileEntry] = []
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name in IGNORED_DIRS:
                continue
            try:
                stat = child.stat()
                entries.append(
                    FileEntry(
                        name=child.name,
                        path=str(child),
                        relative_path=str(child.relative_to(root)),
                        is_directory=child.is_dir(),
                        size_bytes=0 if child.is_dir() else stat.st_size,
                        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    )
                )
            except OSError:
                continue
        return entries

    def search_files(
        self,
        workspace_id: str,
        query: str = "",
        limit: int = 50,
        max_scanned: int = 20_000,
    ) -> list[FileEntry]:
        ws = self.store.workspaces.get(workspace_id)
        if not ws:
            return []
        root = Path(ws.path).resolve()
        q = query.strip().lower()
        results: list[FileEntry] = []
        scanned = 0  # budget so a non-matching query cannot walk a huge tree (e.g. $HOME)

        for dirpath, dirnames, filenames in os.walk(root):
            scanned += 1 + len(filenames)
            if scanned > max_scanned:
                break
            dirnames[:] = [d for d in sorted(dirnames) if d not in IGNORED_DIRS and not d.startswith(".git")]
            current = Path(dirpath)
            rel_dir = "" if current == root else str(current.relative_to(root))

            if rel_dir and (not q or q in rel_dir.lower()):
                results.append(
                    FileEntry(
                        name=current.name,
                        path=str(current),
                        relative_path=rel_dir,
                        is_directory=True,
                    )
                )
                if len(results) >= limit:
                    return results

            for fname in sorted(filenames):
                full = current / fname
                rel = str(full.relative_to(root))
                if not q or q in rel.lower():
                    try:
                        size = full.stat().st_size
                    except OSError:
                        size = 0
                    results.append(
                        FileEntry(
                            name=fname,
                            path=str(full),
                            relative_path=rel,
                            is_directory=False,
                            size_bytes=size,
                        )
                    )
                    if len(results) >= limit:
                        return results
        return results

    def read_file_content(self, workspace_id: str, rel_path: str, max_bytes: int = 120_000) -> str:
        ws = self.store.workspaces.get(workspace_id)
        if not ws:
            raise KeyError("Workspace not found")
        target = self._safe_resolve_inside(ws, rel_path)
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(rel_path)
        raw = target.read_bytes()[:max_bytes]
        return raw.decode("utf-8", errors="replace")

    def get_git_diff(self, workspace_id: str) -> GitDiffSummary:
        ws = self.store.workspaces.get(workspace_id)
        if not ws:
            raise KeyError("Workspace not found")
        self._refresh_git_meta(ws)
        if not ws.is_git_repo:
            return GitDiffSummary(workspace_id=workspace_id, branch=None, files=[])

        root = ws.path
        # -z: NUL-separated, never quoted/escaped (non-ASCII names, spaces, quotes).
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=off", "-C", root, "status", "--porcelain=v1", "-z"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        diff_files: list[DiffFile] = []
        total_add = 0
        total_del = 0

        entries = proc.stdout.split("\0")
        idx = 0
        while idx < len(entries):
            line = entries[idx]
            idx += 1
            if len(line) < 4:
                continue
            xy = line[:2]
            rel_path = line[3:]
            if "R" in xy or "C" in xy:
                idx += 1  # with -z a rename/copy is followed by its original path
            status_code = xy.strip() or "M"
            staged = xy[0] not in (" ", "?")

            patch = ""
            if status_code == "??":
                full = Path(root) / rel_path
                # Never follow symlinks / paths that leave the workspace root.
                try:
                    inside = not full.is_symlink() and bool(self._safe_resolve_inside(ws, rel_path))
                except (PermissionError, ValueError, OSError):
                    inside = False
                if inside and full.is_file():
                    text = full.read_text(encoding="utf-8", errors="replace")[:32_000]
                    lines = text.splitlines()
                    patch = f"--- /dev/null\n+++ b/{rel_path}\n@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(
                        f"+{ln}" for ln in lines
                    )
            else:
                dproc = subprocess.run(
                    ["git", "-c", "core.quotepath=off", "-C", root, "diff", "HEAD", "--", rel_path],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    check=False,
                )
                patch = dproc.stdout
                if not patch:
                    sproc = subprocess.run(
                        ["git", "-c", "core.quotepath=off", "-C", root, "diff", "--cached", "--", rel_path],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=5,
                        check=False,
                    )
                    patch = sproc.stdout

            adds = sum(1 for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
            dels = sum(1 for ln in patch.splitlines() if ln.startswith("-") and not ln.startswith("---"))
            total_add += adds
            total_del += dels
            diff_files.append(
                DiffFile(
                    path=rel_path,
                    status=status_code,
                    additions=adds,
                    deletions=dels,
                    patch=patch[:64_000],
                    staged=staged,
                )
            )

        return GitDiffSummary(
            workspace_id=workspace_id,
            branch=ws.git_branch,
            files=diff_files,
            total_additions=total_add,
            total_deletions=total_del,
        )

    def revert_file(self, workspace_id: str, rel_path: str) -> None:
        ws = self.store.workspaces.get(workspace_id)
        if not ws or not ws.is_git_repo:
            raise KeyError("Git workspace not found")
        self._safe_resolve_inside(ws, rel_path)
        tracked = subprocess.run(
            ["git", "-C", ws.path, "ls-files", "--error-unmatch", "--", rel_path],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if tracked.returncode != 0:
            raise ValueError("This file is not tracked by git, so there is nothing to revert.")
        proc = subprocess.run(
            ["git", "-C", ws.path, "checkout", "HEAD", "--", rel_path],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            fallback = subprocess.run(
                ["git", "-C", ws.path, "checkout", "--", rel_path],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if fallback.returncode != 0:
                raise ValueError("Git could not revert this file.")

    def list_artifacts(self, conversation_id: str | None = None, workspace_id: str | None = None) -> list[ArtifactItem]:
        artifacts: list[ArtifactItem] = []

        # 1. Pocket Relay Bridge artifacts folder
        bridge_artifacts_dir = self.store.data_dir / "artifacts"
        bridge_artifacts_dir.mkdir(parents=True, exist_ok=True)
        for p in sorted(bridge_artifacts_dir.rglob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:40]:
            artifacts.append(self._artifact_from_file(p, conversation_id=p.parent.name if p.parent != bridge_artifacts_dir else None))

        # 2. Local Antigravity desktop brain artifacts (~/.gemini/antigravity/brain/<conv_id>/*.md)
        brain_root = Path.home() / ".gemini" / "antigravity" / "brain"
        # conversation_id is client input: only a plain id may be used as a directory name.
        if conversation_id and not re.fullmatch(r"[A-Za-z0-9_-]+", conversation_id):
            conversation_id = None
        if brain_root.exists():
            dirs = [brain_root / conversation_id] if conversation_id and (brain_root / conversation_id).exists() else sorted(
                (d for d in brain_root.iterdir() if d.is_dir()),
                key=lambda d: d.stat().st_mtime,
                reverse=True,
            )[:12]
            for d in dirs:
                for p in sorted(d.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:8]:
                    artifacts.append(self._artifact_from_file(p, conversation_id=d.name))

        # 3. Workspace docs / plan files if workspace specified
        if workspace_id and workspace_id in self.store.workspaces:
            ws_root = Path(self.store.workspaces[workspace_id].path)
            for candidate in ("README.md", "PLAN.md", "IMPLEMENTATION_PLAN.md", "WALKTHROUGH.md"):
                fp = ws_root / candidate
                if fp.exists() and fp.is_file():
                    artifacts.append(self._artifact_from_file(fp, conversation_id=conversation_id))

        if conversation_id:
            filtered = [a for a in artifacts if a.conversation_id in (conversation_id, None)]
            if filtered:
                artifacts = filtered

        # Deduplicate by path
        seen: set[str] = set()
        unique: list[ArtifactItem] = []
        for a in artifacts:
            if a.path not in seen:
                seen.add(a.path)
                unique.append(a)
        unique.sort(key=lambda a: a.updated_at, reverse=True)
        return unique[:50]

    def save_artifact(
        self,
        title: str,
        content: str,
        conversation_id: str | None = None,
        kind: str = "markdown",
        request_feedback: bool = False,
    ) -> ArtifactItem:
        folder = self.store.data_dir / "artifacts" / (conversation_id or "global")
        folder.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in title).strip("_") or "artifact"
        if not safe_name.endswith(".md"):
            safe_name += ".md"
        fp = folder / safe_name
        fp.write_text(content, encoding="utf-8")
        item = self._artifact_from_file(fp, conversation_id=conversation_id)
        item.kind = kind
        item.request_feedback = request_feedback
        return item

    def _artifact_from_file(self, fp: Path, conversation_id: str | None = None) -> ArtifactItem:
        stat = fp.stat()
        text = fp.read_text(encoding="utf-8", errors="replace")[:80_000]
        lower_name = fp.name.lower()
        kind = "plan" if "plan" in lower_name else "walkthrough" if "walkthrough" in lower_name else "markdown"
        first_line = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.strip()), fp.stem)
        art_id = hashlib.sha256(str(fp).encode()).hexdigest()[:16]
        return ArtifactItem(
            id=art_id,
            title=fp.name,
            kind=kind,
            path=str(fp),
            conversation_id=conversation_id,
            summary=first_line[:160],
            content=text,
            request_feedback=(kind == "plan"),
            updated_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )
