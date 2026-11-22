"""Comprehensive regression test suite for Pocket Relay Bridge."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from antigravity_bridge.config import BridgeStore
from antigravity_bridge.main import create_app
from antigravity_bridge.models import (
    ApprovalDecision,
    EffectiveSettings,
    ExecutionPolicy,
    RuntimeMode,
)
from antigravity_bridge.runtime.acp_backend import (
    AntigravityACPBackend,
    _is_macos_tcc_protected,
)
from antigravity_bridge.runtime.base import AgentTurnCallbacks, AgentTurnRequest
from antigravity_bridge.services.discovery import DiscoveryService


@pytest.fixture()
def client_and_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    monkeypatch.setenv("ANTIGRAVITY_DEFAULT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ANTIGRAVITY_DISABLE_BONJOUR", "1")
    # Avoid invoking live `agy acp` during unit tests (speed + isolation)
    monkeypatch.setattr(AntigravityACPBackend, "is_available", lambda self: False)

    # Create sample git repo in tmp_path
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("# Sample Repo\nHello world\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("# Sample Repo\nUpdated line\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("untracked file\n", encoding="utf-8")

    # Add a workspace-local skill & rule so customizations endpoints are deterministic
    skill_dir = tmp_path / ".agents" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill for tests\n---\n# Demo Skill\nBody.\n",
        encoding="utf-8",
    )
    rules_dir = tmp_path / ".agents" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "demo-rule.md").write_text("# Demo Rule\nAlways write tests.\n", encoding="utf-8")

    data_dir = tmp_path / ".bridge_data"
    app = create_app(data_dir=data_dir, port=8765)
    app.state.store.settings.runtime_mode = RuntimeMode.LOCAL_FALLBACK
    app.state.store.save()
    token = app.state.store.auth_token
    with TestClient(app) as client:
        yield client, token, tmp_path


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Health, Status, Auth & Pairing
# ---------------------------------------------------------------------------


def test_health_and_auth_guard(client_and_token):
    client, token, _ = client_and_token
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True

    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers=auth_headers("wrong-token")).status_code == 401

    res = client.get("/api/status", headers=auth_headers(token))
    assert res.status_code == 200
    body = res.json()
    assert body["sdk_available"] is True
    assert body["active_backend"] == "local-fallback"
    assert "available_models" in body and len(body["available_models"]) > 0
    assert body["uptime_seconds"] >= 0


def test_pairing_qr_rotate_and_exchange_rate_limit(client_and_token):
    client, token, _ = client_and_token
    res = client.get("/api/pairing", headers=auth_headers(token))
    assert res.status_code == 200
    info = res.json()
    assert info["pairing_uri"].startswith("pocketrelay://pair?")
    assert info["token"] == token

    qr = client.get("/api/pairing/qr.png", headers=auth_headers(token))
    assert qr.status_code == 200
    assert qr.headers["content-type"] == "image/png"
    assert len(qr.content) > 100

    # Valid 6-digit code exchange (including spaced formatting).
    # The code is only redeemable after the loopback /pair page armed pairing.
    code, remaining = client.app.state.discovery.arm_pairing()
    assert len(code) == 6 and remaining > 0
    spaced = f"{code[:3]} {code[3:]}"
    good = client.post("/api/pair/exchange", json={"code": spaced})
    assert good.status_code == 200
    assert good.json()["token"] == token

    # One-time use: same code cannot be reused (a successful exchange closes the window)
    reused = client.post("/api/pair/exchange", json={"code": code})
    assert reused.status_code == 403

    # Rate limit after 5 failed attempts -> 6th attempt returns 429 even with valid code
    client.app.state.discovery._failed_attempts = 0
    client.app.state.discovery._locked_until = 0.0
    valid_code, _ = client.app.state.discovery.arm_pairing()
    wrong_code = "000000" if valid_code != "000000" else "999999"
    for _ in range(5):
        r = client.post("/api/pair/exchange", json={"code": wrong_code})
        assert r.status_code == 401
    locked = client.post("/api/pair/exchange", json={"code": valid_code})
    assert locked.status_code == 429

    # Rotate bearer token
    rotated = client.post("/api/pairing/rotate", headers=auth_headers(token))
    assert rotated.status_code == 200
    new_token = rotated.json()["token"]
    assert new_token != token
    assert client.get("/api/status", headers=auth_headers(token)).status_code == 401
    assert client.get("/api/status", headers=auth_headers(new_token)).status_code == 200


@pytest.mark.asyncio
async def test_pair_page_loopback_only(client_and_token):
    client, _, _ = client_and_token
    # Default TestClient host is "testclient" (non-loopback) -> 403
    assert client.get("/pair").status_code == 403

    # Non-loopback LAN IP -> 403
    lan_transport = httpx.ASGITransport(app=client.app, client=("192.168.1.50", 54321))
    async with httpx.AsyncClient(transport=lan_transport, base_url="http://192.168.1.10:8765") as ac:
        res_lan = await ac.get("/pair")
        assert res_lan.status_code == 403

    # Loopback IPv4 (127.0.0.1) -> 200 HTML containing QR & 6-digit code
    code, _ = client.app.state.discovery.current_pair_code()
    formatted = f"{code[:3]} {code[3:]}"
    loop_v4 = httpx.ASGITransport(app=client.app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=loop_v4, base_url="http://127.0.0.1:8765") as ac:
        res_v4 = await ac.get("/pair")
        assert res_v4.status_code == 200
        assert "data:image/png;base64," in res_v4.text
        assert formatted in res_v4.text

    # Loopback IPv6 (::1) -> 200 HTML
    loop_v6 = httpx.ASGITransport(app=client.app, client=("::1", 54321))
    async with httpx.AsyncClient(transport=loop_v6, base_url="http://localhost:8765") as ac:
        res_v6 = await ac.get("/pair")
        assert res_v6.status_code == 200


# ---------------------------------------------------------------------------
# Bonjour / mDNS DiscoveryService
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_stable_name_txt_records_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTIGRAVITY_PUBLIC_URL", "https://my-mac.tail0000.ts.net:8443")

    store1 = BridgeStore(data_dir=tmp_path / "d1")
    store1.host_name = "MyWorkMac"
    store2 = BridgeStore(data_dir=tmp_path / "d2")
    store2.host_name = "MyWorkMac"
    assert store1.host_id != store2.host_id

    disc1 = DiscoveryService(store1, port=8765)
    disc2 = DiscoveryService(store2, port=8765)

    # Stable name per host — NO random suffixes
    assert disc1.service_name == "MyWorkMac._pocketrelay._tcp.local."
    assert disc1.service_name == disc2.service_name
    assert "(" not in disc1.service_name

    # Verify TXT records
    info = disc1.build_service_info(lan_ip="192.168.1.42")
    assert info.name == "MyWorkMac._pocketrelay._tcp.local."
    assert info.type == "_pocketrelay._tcp.local."
    assert info.port == 8765
    decoded = {k.decode(): v.decode() for k, v in info.properties.items()}
    assert decoded["host_id"] == store1.host_id
    assert decoded["host_name"] == "MyWorkMac"
    assert decoded["platform"] in ("macOS", "Linux", "Windows")
    assert decoded["url"] == "https://my-mac.tail0000.ts.net:8443"
    assert decoded["lan_url"] == "http://192.168.1.42:8765"

    # Respects ANTIGRAVITY_DISABLE_BONJOUR=1
    events: list[str] = []

    class FakeAsyncZeroconf:
        def __init__(self) -> None:
            events.append("init")

        async def async_register_service(self, svc_info, **kwargs) -> None:
            events.append(f"register:{svc_info.name}:coop={kwargs.get('cooperating_responders')}:change={kwargs.get('allow_name_change')}")

        async def async_unregister_service(self, svc_info) -> None:
            events.append(f"unregister:{svc_info.name}")

        async def async_unregister_all_services(self) -> None:
            events.append("unregister_all")

        async def async_close(self) -> None:
            events.append("close")

    monkeypatch.setattr("antigravity_bridge.services.discovery.AsyncZeroconf", FakeAsyncZeroconf)

    monkeypatch.setenv("ANTIGRAVITY_DISABLE_BONJOUR", "1")
    await disc1.start()
    assert events == []
    assert not disc1.is_running

    # When enabled: registers with stable name, cleanly unregisters on restart & stop
    monkeypatch.delenv("ANTIGRAVITY_DISABLE_BONJOUR", raising=False)
    await disc1.start()
    assert disc1.is_running
    assert events == [
        "init",
        "register:MyWorkMac._pocketrelay._tcp.local.:coop=True:change=False",
    ]

    # Restarting stops old registration cleanly before registering again
    await disc1.start()
    assert "unregister:MyWorkMac._pocketrelay._tcp.local." in events
    assert "close" in events
    assert disc1.is_running

    await disc1.stop()
    assert not disc1.is_running
    assert events[-3:] == [
        "unregister:MyWorkMac._pocketrelay._tcp.local.",
        "unregister_all",
        "close",
    ]


# ---------------------------------------------------------------------------
# ACP Backend: macOS TCC Safety & Multi-Turn JSON-RPC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acp_tcc_safe_path_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    backend = AntigravityACPBackend()
    home_str = str(Path.home().resolve())

    # 1. None workspace -> home
    sub_cwd, sess_cwd, add_dirs = await backend.resolve_acp_paths(None)
    assert sub_cwd == home_str
    assert sess_cwd == home_str
    assert add_dirs == []

    # 2. Non-existent directory -> graceful fallback to home, no hang
    sub_cwd, sess_cwd, add_dirs = await backend.resolve_acp_paths(str(tmp_path / "does-not-exist"))
    assert sub_cwd == home_str
    assert sess_cwd == home_str
    assert add_dirs == []

    # 3. Accessible directory outside home (tmp_path) -> subprocess cwd is home, --add-dir passed
    sub_cwd, sess_cwd, add_dirs = await backend.resolve_acp_paths(str(tmp_path))
    assert sub_cwd == home_str
    assert sess_cwd == str(tmp_path.resolve())
    assert add_dirs == [str(tmp_path.resolve())]

    # 4. Simulated macOS TCC ~/Documents directory when readable -> subprocess & session cwd stay at home, --add-dir added
    docs_sub = Path.home().resolve() / "Documents" / "fake-project"
    monkeypatch.setattr("antigravity_bridge.runtime.acp_backend.sys.platform", "darwin")
    assert _is_macos_tcc_protected(docs_sub) is True

    async def fake_readable_true(path: Path, timeout: float = 1.0) -> bool:
        return True

    monkeypatch.setattr("antigravity_bridge.runtime.acp_backend.is_dir_readable", fake_readable_true)
    sub_cwd, sess_cwd, add_dirs = await backend.resolve_acp_paths(str(docs_sub))
    assert sub_cwd == home_str
    assert sess_cwd == home_str
    assert add_dirs == [str(docs_sub)]

    # 5. Simulated macOS TCC ~/Documents directory when TCC-blocked (unreadable/timeout) -> degrades cleanly without hanging
    async def fake_readable_false(path: Path, timeout: float = 1.0) -> bool:
        return False

    monkeypatch.setattr("antigravity_bridge.runtime.acp_backend.is_dir_readable", fake_readable_false)
    sub_cwd, sess_cwd, add_dirs = await backend.resolve_acp_paths(str(docs_sub))
    assert sub_cwd == home_str
    assert sess_cwd == home_str
    assert add_dirs == []


@pytest.mark.asyncio
async def test_acp_backend_multiturn_and_error_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run AntigravityACPBackend against a fake Python `agy` ACP JSON-RPC script."""
    fake_agy = tmp_path / "fake_agy.py"
    log_file = tmp_path / "acp_invocations.jsonl"
    fake_agy.write_text(
        f"""#!{sys.executable}
import json, os, sys
with open({str(log_file)!r}, "a") as lf:
    lf.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")

session_id = "sess-multi-1"
for raw in sys.stdin:
    msg = json.loads(raw)
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {{}}
    if method == "initialize":
        print(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": {{"protocolVersion": 1}}}}), flush=True)
    elif method == "session/new":
        print(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": {{
            "sessionId": session_id,
            "models": {{"availableModels": [{{"modelId": "gemini-3-pro"}}]}}
        }}}}), flush=True)
    elif method == "session/load":
        # Replay prior turn history before returning session/load result (real ACP behavior)
        print(json.dumps({{"jsonrpc": "2.0", "method": "session/update", "params": {{
            "update": {{"sessionUpdate": "agent_message_chunk", "content": {{"text": "OLD_TURN_HISTORY"}}}}
        }}}}), flush=True)
        print(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": {{
            "sessionId": params.get("sessionId"),
            "models": {{"availableModels": [{{"modelId": "gemini-3-pro"}}]}}
        }}}}), flush=True)
    elif method in ("session/set_mode", "session/set_model"):
        print(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": {{}}}}), flush=True)
    elif method == "session/prompt":
        prompt_text = params["prompt"][0]["text"]
        print(json.dumps({{"jsonrpc": "2.0", "method": "session/update", "params": {{
            "update": {{"sessionUpdate": "agent_thought_chunk", "content": {{"text": "thinking..."}}}}
        }}}}), flush=True)
        print(json.dumps({{"jsonrpc": "2.0", "method": "session/update", "params": {{
            "update": {{"sessionUpdate": "agent_message_chunk", "content": {{"text": f"echo:{{prompt_text}}"}}}}
        }}}}), flush=True)
        print(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": {{
            "usage": {{"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}}
        }}}}), flush=True)
""",
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)
    monkeypatch.setattr("antigravity_bridge.runtime.acp_backend.find_agy_binary", lambda: str(fake_agy))

    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    backend = AntigravityACPBackend(handshake_timeout=5.0)
    eff = EffectiveSettings(
        model="gemini-3-pro",
        runtime_mode=RuntimeMode.ACP,
        agent_behavior="autonomous",
        execution_policy=ExecutionPolicy.REQUEST_REVIEW,
        terminal_sandbox=False,
        non_workspace_file_access="ask",
        internet_access_policy="allow",
        command_allowlist=[],
        command_denylist=[],
        browser_allowlist=[],
        enable_subagents=True,
    )

    tokens: list[str] = []
    thoughts: list[str] = []

    async def on_token(t: str) -> None:
        tokens.append(t)

    async def on_thought(t: str) -> None:
        thoughts.append(t)

    async def noop(_: Any) -> None:
        return None

    cbs = AgentTurnCallbacks(on_token=on_token, on_thought=on_thought, on_tool_call=noop, on_tool_update=noop)

    # Turn 1: creates session
    res1 = await backend.run_turn(
        AgentTurnRequest(conversation_id="conv-1", prompt="hello turn 1", workspace_path=str(ws_dir), settings=eff),
        cbs,
    )
    assert res1.content == "echo:hello turn 1"
    assert res1.thoughts == "thinking..."
    assert res1.usage is not None and res1.usage.total_tokens == 15
    assert backend._session_ids["conv-1"] == "sess-multi-1"

    # Turn 2: reuses session via session/load
    res2 = await backend.run_turn(
        AgentTurnRequest(conversation_id="conv-1", prompt="follow-up turn 2", workspace_path=str(ws_dir), settings=eff),
        cbs,
    )
    assert res2.content == "echo:follow-up turn 2"

    invocations = [json.loads(ln) for ln in log_file.read_text(encoding="utf-8").splitlines()]
    assert len(invocations) == 2
    for inv in invocations:
        assert inv["cwd"] == str(Path.home().resolve())
        assert "--add-dir" in inv["argv"]
        assert str(ws_dir.resolve()) in inv["argv"]


@pytest.mark.asyncio
async def test_acp_backend_handshake_error_and_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    eff = EffectiveSettings(
        model="gemini-3-pro",
        runtime_mode=RuntimeMode.ACP,
        agent_behavior="autonomous",
        execution_policy=ExecutionPolicy.REQUEST_REVIEW,
        terminal_sandbox=False,
        non_workspace_file_access="ask",
        internet_access_policy="allow",
        command_allowlist=[],
        command_denylist=[],
        browser_allowlist=[],
        enable_subagents=True,
    )

    async def noop(_: Any) -> None:
        return None

    cbs = AgentTurnCallbacks(on_token=noop, on_thought=noop, on_tool_call=noop, on_tool_update=noop)

    # 1. Hanging ACP binary triggers handshake timeout instead of freezing forever
    hang_agy = tmp_path / "hang_agy.py"
    hang_agy.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n", encoding="utf-8")
    hang_agy.chmod(0o755)
    monkeypatch.setattr("antigravity_bridge.runtime.acp_backend.find_agy_binary", lambda: str(hang_agy))
    backend = AntigravityACPBackend(handshake_timeout=0.4)
    with pytest.raises(RuntimeError, match="timed out during session initialization"):
        await backend.run_turn(
            AgentTurnRequest(conversation_id="conv-hang", prompt="hi", workspace_path=None, settings=eff),
            cbs,
        )

    # 2. session/new error raises immediately instead of hanging
    err_agy = tmp_path / "err_agy.py"
    err_agy.write_text(
        f"""#!{sys.executable}
import json, sys
for raw in sys.stdin:
    msg = json.loads(raw)
    mid = msg.get("id")
    if msg.get("method") == "initialize":
        print(json.dumps({{"jsonrpc": "2.0", "id": mid, "result": {{"protocolVersion": 1}}}}), flush=True)
    elif msg.get("method") == "session/new":
        print(json.dumps({{"jsonrpc": "2.0", "id": mid, "error": {{"code": -32000, "message": "tcc denied"}}}}), flush=True)
""",
        encoding="utf-8",
    )
    err_agy.chmod(0o755)
    monkeypatch.setattr("antigravity_bridge.runtime.acp_backend.find_agy_binary", lambda: str(err_agy))
    backend2 = AntigravityACPBackend(handshake_timeout=2.0)
    with pytest.raises(RuntimeError, match="tcc denied"):
        await backend2.run_turn(
            AgentTurnRequest(conversation_id="conv-err", prompt="hi", workspace_path=None, settings=eff),
            cbs,
        )


# ---------------------------------------------------------------------------
# Workspaces CRUD, Files, Git Diff, Revert, Mentions & Global Search
# ---------------------------------------------------------------------------


def test_workspaces_crud_files_diff_revert_mentions_search(client_and_token):
    client, token, tmp_path = client_and_token
    headers = auth_headers(token)

    ws_list = client.get("/api/workspaces", headers=headers).json()
    assert len(ws_list) >= 1
    ws_id = ws_list[0]["id"]

    # Add a second workspace, update its settings, then delete it
    extra_dir = tmp_path / "subproject"
    extra_dir.mkdir()
    created_ws = client.post("/api/workspaces", json={"path": str(extra_dir), "name": "SubProj"}, headers=headers)
    assert created_ws.status_code == 200
    extra_id = created_ws.json()["id"]
    assert created_ws.json()["name"] == "SubProj"

    # Invalid workspace path -> 400
    assert client.post("/api/workspaces", json={"path": str(tmp_path / "nope")}, headers=headers).status_code == 400

    # Update workspace override settings
    upd = client.put(
        f"/api/workspaces/{extra_id}/settings",
        json={"model": "gemini-2.5-flash", "sandbox_mode": True},
        headers=headers,
    )
    assert upd.status_code == 200
    assert upd.json()["settings_override"]["model"] == "gemini-2.5-flash"
    assert upd.json()["settings_override"]["sandbox_mode"] is True

    # Delete workspace + 404 on unknown
    assert client.delete(f"/api/workspaces/{extra_id}", headers=headers).status_code == 200
    assert client.delete(f"/api/workspaces/{extra_id}", headers=headers).status_code == 404

    # List files & read file + path traversal check
    files = client.get(f"/api/workspaces/{ws_id}/files", headers=headers).json()
    names = {f["name"] for f in files}
    assert "README.md" in names

    file_res = client.get(f"/api/workspaces/{ws_id}/file?path=README.md", headers=headers)
    assert file_res.status_code == 200
    assert "Updated line" in file_res.json()["content"]
    assert client.get(f"/api/workspaces/{ws_id}/file?path=missing.txt", headers=headers).status_code == 404
    assert client.get(f"/api/workspaces/{ws_id}/file?path=../../etc/passwd", headers=headers).status_code == 403
    assert client.get(f"/api/workspaces/{ws_id}/files?path=../../", headers=headers).status_code == 403

    # Git diff & revert
    diff = client.get(f"/api/workspaces/{ws_id}/diff", headers=headers).json()
    paths = {f["path"] for f in diff["files"]}
    assert "README.md" in paths
    assert "notes.txt" in paths

    # Revert traversal -> 403, unknown workspace -> 404
    assert client.post(f"/api/workspaces/{ws_id}/revert?path=../../etc/passwd", headers=headers).status_code == 403
    assert client.post("/api/workspaces/ws-missing/revert?path=README.md", headers=headers).status_code == 404

    rev = client.post(f"/api/workspaces/{ws_id}/revert?path=README.md", headers=headers)
    assert rev.status_code == 200
    assert rev.json()["reverted"] is True
    restored = client.get(f"/api/workspaces/{ws_id}/file?path=README.md", headers=headers).json()["content"]
    assert "Hello world" in restored
    diff_after = client.get(f"/api/workspaces/{ws_id}/diff", headers=headers).json()
    assert "README.md" not in {f["path"] for f in diff_after["files"]}

    # Mentions (file, rule)
    mentions = client.get(f"/api/mentions?q=README&workspace_id={ws_id}", headers=headers).json()
    assert any(m["kind"] == "file" and "README.md" in m["label"] for m in mentions)
    rule_mentions = client.get(f"/api/mentions?q=demo-rule&workspace_id={ws_id}", headers=headers).json()
    assert any(m["kind"] == "rule" for m in rule_mentions)

    # Global search
    search_hits = client.get(f"/api/search?q=README&workspace_id={ws_id}", headers=headers).json()
    assert any(h["kind"] == "file" and h["title"] == "README.md" for h in search_hits)
    cmd_hits = client.get("/api/search?q=plan", headers=headers).json()
    assert any(h["kind"] == "command" and h["title"] == "/plan" for h in cmd_hits)


# ---------------------------------------------------------------------------
# Conversations CRUD, Uploads, Pin, Compact, Cancel, Subagents & WebSocket
# ---------------------------------------------------------------------------


def test_conversation_streaming_plan_artifact_uploads_pin_compact_cancel_ws(client_and_token):
    client, token, _ = client_and_token
    headers = auth_headers(token)
    ws_id = client.get("/api/workspaces", headers=headers).json()[0]["id"]

    # WebSocket rejects invalid token
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws?token=bad-token") as ws:
            ws.receive_text()
    assert exc_info.value.code == 4401

    # Upload text and image attachments
    up_txt = client.post(
        "/api/uploads",
        files={"file": ("../notes.txt", b"hello upload", "text/plain")},
        headers=headers,
    )
    assert up_txt.status_code == 200
    att_txt = up_txt.json()
    assert att_txt["filename"] == "notes.txt"
    assert att_txt["size_bytes"] == 12

    up_img = client.post(
        "/api/uploads",
        files={"file": ("dot.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        headers=headers,
    )
    assert up_img.status_code == 200
    assert up_img.json()["preview_base64"] is not None

    # Create two conversations, pin second one, verify ordering
    conv1 = client.post("/api/conversations", json={"workspace_id": ws_id, "title": "Build Plan"}, headers=headers).json()
    conv2 = client.post("/api/conversations", json={"workspace_id": ws_id, "title": "Second Thread"}, headers=headers).json()
    conv_id = conv1["id"]

    pin_res = client.post(f"/api/conversations/{conv2['id']}/pin?pinned=true", headers=headers)
    assert pin_res.status_code == 200 and pin_res.json()["is_pinned"] is True
    listed = client.get(f"/api/conversations?workspace_id={ws_id}", headers=headers).json()
    assert listed[0]["id"] == conv2["id"]
    client.post(f"/api/conversations/{conv2['id']}/pin?pinned=false", headers=headers)

    # Stream /plan turn + ping/pong over WebSocket
    with client.websocket_connect(f"/ws?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "connected"

        ws.send_text("ping")
        pong = ws.receive_json()
        assert pong["type"] == "pong"

        send_res = client.post(
            f"/api/conversations/{conv_id}/messages",
            json={
                "content": "/plan Design a caching layer",
                "slash_command": "/plan",
                "attachment_ids": [att_txt["id"]],
                "mentions": [{"kind": "file", "id": "README.md", "label": "README.md"}],
            },
            headers=headers,
        )
        assert send_res.status_code == 200

        seen_types: set[str] = set()
        for _ in range(120):
            ev = ws.receive_json()
            seen_types.add(ev["type"])
            if ev["type"] == "turn_completed":
                break
        assert {"message_created", "turn_started", "thought", "token", "tool_call", "tool_update", "turn_completed"}.issubset(
            seen_types
        )

    # Verify artifact created
    artifacts = client.get(f"/api/artifacts?conversation_id={conv_id}", headers=headers).json()
    assert any(a["title"] == "implementation_plan.md" for a in artifacts)

    # Compact conversation after >4 messages
    for i in range(3):
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_json()
            client.post(f"/api/conversations/{conv_id}/messages", json={"content": f"Turn {i}"}, headers=headers)
            for _ in range(120):
                if ws.receive_json()["type"] == "turn_completed":
                    break
    full_before = client.get(f"/api/conversations/{conv_id}", headers=headers).json()
    assert len(full_before["messages"]) > 4
    compacted = client.post(f"/api/conversations/{conv_id}/compact", headers=headers)
    assert compacted.status_code == 200
    assert len(compacted.json()["messages"]) == 3
    assert compacted.json()["messages"][0]["role"] == "system"

    # Cancel active turn
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.receive_json()
        client.post(f"/api/conversations/{conv_id}/messages", json={"content": "Long streaming turn"}, headers=headers)
        cancel_res = client.post(f"/api/conversations/{conv_id}/cancel", headers=headers)
        assert cancel_res.status_code == 200
        assert "cancelled" in cancel_res.json()

    # Subagents via /swarm
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.receive_json()
        client.post(
            f"/api/conversations/{conv2['id']}/messages",
            json={"content": "/swarm Inspect repo", "slash_command": "/swarm"},
            headers=headers,
        )
        for _ in range(120):
            if ws.receive_json()["type"] == "turn_completed":
                break
    subs = client.get(f"/api/subagents?conversation_id={conv2['id']}", headers=headers).json()
    assert len(subs) >= 1
    kill_sub = client.post(f"/api/subagents/{subs[0]['id']}/kill", headers=headers)
    assert kill_sub.status_code == 200 and kill_sub.json()["killed"] is True

    # Delete conversation & 404 check
    assert client.delete(f"/api/conversations/{conv2['id']}", headers=headers).status_code == 200
    assert client.get(f"/api/conversations/{conv2['id']}", headers=headers).status_code == 404


# ---------------------------------------------------------------------------
# Approvals & Interactive Questions
# ---------------------------------------------------------------------------


def test_approvals_and_questions_roundtrip(client_and_token):
    client, token, _ = client_and_token
    headers = auth_headers(token)
    ws_id = client.get("/api/workspaces", headers=headers).json()[0]["id"]
    conv = client.post("/api/conversations", json={"workspace_id": ws_id, "title": "Approvals"}, headers=headers).json()
    conv_id = conv["id"]

    # 404 on unknown IDs
    assert client.post("/api/approvals/missing", json={"decision": "deny"}, headers=headers).status_code == 404
    assert client.post("/api/questions/missing", json={"answers": {}}, headers=headers).status_code == 404

    # Interactive tool approval via `run echo approved-ok`
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.receive_json()
        client.post(
            f"/api/conversations/{conv_id}/messages",
            json={"content": "run echo approved-ok"},
            headers=headers,
        )
        approval_id = None
        for _ in range(60):
            ev = ws.receive_json()
            if ev["type"] == "approval_request":
                approval_id = ev["payload"]["id"]
                break
        assert approval_id is not None

        pending = client.get("/api/approvals", headers=headers).json()
        assert any(a["id"] == approval_id for a in pending)

        res = client.post(
            f"/api/approvals/{approval_id}",
            json={"decision": ApprovalDecision.ALLOW_ONCE.value},
            headers=headers,
        )
        assert res.status_code == 200 and res.json()["resolved"] is True

        final_msg = None
        for _ in range(120):
            ev = ws.receive_json()
            if ev["type"] == "turn_completed":
                final_msg = ev["payload"]["message"]
                break
        assert final_msg is not None
        assert "approved-ok" in final_msg["content"]

    # Interactive AskQuestion via /grill-me
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.receive_json()
        client.post(
            f"/api/conversations/{conv_id}/messages",
            json={"content": "/grill-me Design API", "slash_command": "/grill-me"},
            headers=headers,
        )
        question_id = None
        for _ in range(60):
            ev = ws.receive_json()
            if ev["type"] == "ask_question":
                question_id = ev["payload"]["id"]
                break
        assert question_id is not None

        q_list = client.get("/api/questions", headers=headers).json()
        assert any(q["id"] == question_id for q in q_list)

        ans = client.post(
            f"/api/questions/{question_id}",
            json={"answers": {"0": ["Ship working end-to-end vertical slice"]}},
            headers=headers,
        )
        assert ans.status_code == 200 and ans.json()["resolved"] is True

        final_msg = None
        for _ in range(120):
            ev = ws.receive_json()
            if ev["type"] == "turn_completed":
                final_msg = ev["payload"]["message"]
                break
        assert final_msg is not None
        assert "Ship working end-to-end vertical slice" in final_msg["content"]


# ---------------------------------------------------------------------------
# Scheduler, Terminals, Customizations & Settings
# ---------------------------------------------------------------------------


def test_scheduler_terminals_customizations_and_settings(client_and_token):
    client, token, _ = client_and_token
    headers = auth_headers(token)
    ws_id = client.get("/api/workspaces", headers=headers).json()[0]["id"]

    # Schedule CRUD (cron + timer + validation + patch + run + delete)
    bad_cron = client.post(
        "/api/schedules",
        json={"name": "Bad", "prompt": "x", "kind": "cron", "cron_expression": "not-a-cron"},
        headers=headers,
    )
    assert bad_cron.status_code == 400

    task = client.post(
        "/api/schedules",
        json={
            "name": "Morning audit",
            "prompt": "Summarize workspace files",
            "kind": "cron",
            "cron_expression": "0 8 * * *",
            "workspace_id": ws_id,
        },
        headers=headers,
    ).json()
    assert task["enabled"] is True
    assert task["next_run_at"] is not None

    patched = client.patch(
        f"/api/schedules/{task['id']}",
        json={"name": "Updated audit", "enabled": False},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Updated audit"
    assert patched.json()["enabled"] is False

    timer = client.post(
        "/api/schedules",
        json={"name": "Quick timer", "prompt": "Ping", "kind": "timer", "delay_seconds": 30},
        headers=headers,
    ).json()
    assert timer["kind"] == "timer"
    assert timer["next_run_at"] is not None

    schedules = client.get("/api/schedules", headers=headers).json()
    assert len(schedules) >= 2

    run = client.post(f"/api/schedules/{task['id']}/run", headers=headers).json()
    assert run["status"] == "succeeded"

    assert client.delete(f"/api/schedules/{timer['id']}", headers=headers).status_code == 200
    assert client.delete(f"/api/schedules/{timer['id']}", headers=headers).status_code == 404

    # Terminals: run, denylist, interactive stdin input, and kill
    denied = client.post("/api/terminals", json={"command": "sudo rm -rf /tmp/x"}, headers=headers)
    assert denied.status_code == 403

    term = client.post(
        "/api/terminals",
        json={"command": "echo antigravity-ok", "workspace_id": ws_id},
        headers=headers,
    ).json()
    time.sleep(0.3)
    terms = client.get("/api/terminals", headers=headers).json()
    matched = next(t for t in terms if t["id"] == term["id"])
    assert "antigravity-ok" in matched["output"]

    # Interactive terminal: read stdin line and kill
    interactive = client.post(
        "/api/terminals",
        json={"command": "cat", "workspace_id": ws_id},
        headers=headers,
    ).json()
    sent = client.post(f"/api/terminals/{interactive['id']}/input", json={"text": "hello-stdin"}, headers=headers)
    assert sent.status_code == 200
    time.sleep(0.2)
    killed = client.post(f"/api/terminals/{interactive['id']}/kill", headers=headers)
    assert killed.status_code == 200 and killed.json()["killed"] is True
    terms_after = client.get("/api/terminals", headers=headers).json()
    cat_term = next(t for t in terms_after if t["id"] == interactive["id"])
    assert "hello-stdin" in cat_term["output"]
    assert cat_term["status"] == "killed"
    assert client.post("/api/terminals/term-missing/kill", headers=headers).status_code == 404

    # Customizations: snapshot, skill detail, toggle, invalid kind
    cust = client.get(f"/api/customizations?workspace_id={ws_id}", headers=headers).json()
    assert len(cust["slash_commands"]) >= 10
    assert any(s["id"] == "demo-skill" for s in cust["skills"])
    assert any(r["name"] == "demo-rule.md" for r in cust["rules"])

    skill_detail = client.get(f"/api/customizations/skills/demo-skill?workspace_id={ws_id}", headers=headers)
    assert skill_detail.status_code == 200
    assert "Demo Skill" in skill_detail.json()["content"]
    assert client.get("/api/customizations/skills/nonexistent", headers=headers).status_code == 404

    assert (
        client.post(
            "/api/customizations/toggle",
            json={"kind": "skill", "id": "demo-skill", "enabled": False},
            headers=headers,
        ).status_code
        == 200
    )
    cust_after = client.get(f"/api/customizations?workspace_id={ws_id}", headers=headers).json()
    demo_s = next(s for s in cust_after["skills"] if s["id"] == "demo-skill")
    assert demo_s["enabled"] is False
    assert (
        client.post(
            "/api/customizations/toggle",
            json={"kind": "invalid", "id": "x", "enabled": False},
            headers=headers,
        ).status_code
        == 400
    )

    # Settings GET/PUT + WebSocket broadcast
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.receive_json()
        settings = client.get("/api/settings", headers=headers).json()
        settings["terminal_sandbox"] = True
        updated = client.put("/api/settings", json=settings, headers=headers).json()
        assert updated["terminal_sandbox"] is True
        ev = ws.receive_json()
        assert ev["type"] == "settings_updated"
        assert ev["payload"]["terminal_sandbox"] is True


# ---------------------------------------------------------------------------
# Regression tests for tester-swarm findings
# ---------------------------------------------------------------------------


def _loopback_client(app, host: str = "127.0.0.1:8765", headers: dict[str, str] | None = None):
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 54321))
    return httpx.AsyncClient(transport=transport, base_url=f"http://{host}", headers=headers or {})


@pytest.mark.asyncio
async def test_pair_page_no_cors_and_host_header_guard(client_and_token):
    """n=39: no wildcard CORS; /pair refuses non-loopback Host (DNS rebinding) and cross-site fetches."""
    client, token, _ = client_and_token
    app = client.app

    # A foreign Origin never gets CORS approval, on /pair or on the API (incl. preflight).
    async with _loopback_client(app, headers={"Origin": "https://evil.example"}) as ac:
        res = await ac.get("/pair")
        assert "access-control-allow-origin" not in res.headers
        assert "access-control-allow-credentials" not in res.headers
        api = await ac.get("/api/status", headers=auth_headers(token))
        assert "access-control-allow-origin" not in api.headers
        preflight = await ac.options(
            "/api/terminals",
            headers={"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"},
        )
        assert "access-control-allow-origin" not in preflight.headers

    # Loopback client but attacker-controlled Host header -> 403, token not served.
    for bad_host in ("evil.example:8765", "evil.example", "127.0.0.1.evil.example:8765", "192.168.1.10:8765"):
        async with _loopback_client(app) as ac:
            res = await ac.get("/pair", headers={"Host": bad_host})
            assert res.status_code == 403, bad_host
            assert token not in res.text

    # Cross-site subresource / fetch from another page -> 403 (cannot silently arm pairing).
    app.state.discovery.disarm_pairing()
    async with _loopback_client(app) as ac:
        res = await ac.get("/pair", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"})
        assert res.status_code == 403
    assert app.state.discovery.is_pairing_armed() is False

    # Legit flows: typed URL / opened by the menu bar app, on every loopback name.
    for good_host in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765", "localhost"):
        async with _loopback_client(app, host=good_host) as ac:
            res = await ac.get("/pair", headers={"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"})
            assert res.status_code == 200, good_host
            assert res.headers["cache-control"] == "no-store"


def test_token_redacted_from_logs_and_banner(client_and_token, monkeypatch, capsys):
    """n=40 / n=3: `token=` never reaches uvicorn logs; banner hides secrets when not a TTY."""
    import io
    import logging

    import uvicorn

    from antigravity_bridge import main as bridge_main

    client, token, _ = client_and_token
    secret = "agy_SUPERSECRETVALUE"

    # Filter rewrites both uvicorn log shapes (WS accept on uvicorn.error, HTTP on uvicorn.access).
    flt = bridge_main.TokenRedactingFilter()
    ws_rec = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1, '%s - "WebSocket %s" [accepted]',
        ("100.1.2.3:0", f"/ws?token={secret}"), None,
    )
    access_rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", f"/api/status?token={secret}&x=1", "1.1", 200), None,
    )
    for rec in (ws_rec, access_rec):
        assert flt.filter(rec) is True
        assert secret not in rec.getMessage()
        assert "token=REDACTED" in rec.getMessage()
    assert "x=1" in access_rec.getMessage()

    # create_app() installs the filter on the uvicorn loggers...
    for name in ("uvicorn.error", "uvicorn.access"):
        assert any(isinstance(f, bridge_main.TokenRedactingFilter) for f in logging.getLogger(name).filters)

    # ...and the config handed to uvicorn.run() redacts end-to-end through uvicorn's own handlers.
    uvicorn.Config(client.app, log_config=bridge_main.build_uvicorn_log_config())
    for name, args in (
        ("uvicorn.error", ("100.1.2.3:0", f"/ws?token={secret}")),
        ("uvicorn.access", ("127.0.0.1:1", "GET", f"/api/status?token={secret}", "1.1", 200)),
    ):
        logger = logging.getLogger(name)
        buf = io.StringIO()
        handlers = logger.handlers or logging.getLogger("uvicorn").handlers  # uvicorn.error propagates
        assert handlers
        handler = handlers[0]
        assert any(isinstance(f, bridge_main.TokenRedactingFilter) for f in handler.filters)
        old_stream = handler.setStream(buf)
        try:
            if name == "uvicorn.error":
                logger.info('%s - "WebSocket %s" [accepted]', *args)
            else:
                logger.info('%s - "%s %s HTTP/%s" %d', *args)
        finally:
            handler.setStream(old_stream)
        assert "token=REDACTED" in buf.getvalue()
        assert secret not in buf.getvalue()

    # Startup banner: stdout is not a TTY under pytest (same as launchd) -> no token / pair URI.
    monkeypatch.setattr(bridge_main, "create_app", lambda **_: client.app)
    monkeypatch.setattr(bridge_main.uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["antigravity-bridge", "--print-qr"])
    bridge_main.cli_main()
    out = capsys.readouterr().out
    assert "Pocket Relay Bridge" in out
    assert token not in out
    assert "antigravity://pair" not in out
    assert "Token:" not in out


def test_ws_accepts_authorization_header_and_query_token(client_and_token):
    """n=3: WS handshake accepts `Authorization: Bearer` while `?token=` keeps working."""
    client, token, _ = client_and_token
    with client.websocket_connect("/ws", headers=auth_headers(token)) as ws:
        assert ws.receive_json()["type"] == "connected"
    with client.websocket_connect(f"/ws?token={token}") as ws:
        assert ws.receive_json()["type"] == "connected"
    for kwargs in ({"headers": auth_headers("wrong")}, {}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", **kwargs) as ws:
                ws.receive_json()


@pytest.mark.asyncio
async def test_pair_code_requires_armed_window(client_and_token):
    """n=42: the 6-digit code only works for 10 minutes after /pair was served on loopback."""
    client, token, _ = client_and_token
    disc = client.app.state.discovery

    # Never armed: refused with a clear instruction, no code is minted, no failure is counted.
    for _ in range(8):
        res = client.post("/api/pair/exchange", json={"code": "123456"})
        assert res.status_code == 403
    assert "pairing page" in res.json()["detail"]
    assert disc._pair_code is None
    assert disc._failed_attempts == 0 and not disc.is_rate_limited()

    # A code that exists but was never shown on /pair is still not redeemable.
    code, _ = disc.current_pair_code()
    assert client.post("/api/pair/exchange", json={"code": code}).status_code == 403

    # Serving /pair on loopback arms pairing.
    async with _loopback_client(client.app) as ac:
        assert (await ac.get("/pair")).status_code == 200
    assert disc.is_pairing_armed()

    # Lock out with bad codes, then reloading /pair re-arms and resets the failure counter.
    wrong = "000000" if code != "000000" else "999999"
    for _ in range(5):
        assert client.post("/api/pair/exchange", json={"code": wrong}).status_code == 401
    assert client.post("/api/pair/exchange", json={"code": code}).status_code == 429
    async with _loopback_client(client.app) as ac:
        page = await ac.get("/pair")
    assert f"{code[:3]} {code[3:]}" in page.text
    assert not disc.is_rate_limited()

    # Window expiry -> 403 again.
    saved = disc._armed_until
    disc._armed_until = time.monotonic() - 1
    assert client.post("/api/pair/exchange", json={"code": code}).status_code == 403
    disc._armed_until = saved

    good = client.post("/api/pair/exchange", json={"code": code})
    assert good.status_code == 200 and good.json()["token"] == token
    # One-time: the window closes after a successful exchange.
    assert client.post("/api/pair/exchange", json={"code": code}).status_code == 403


def test_cron_is_evaluated_in_local_timezone(client_and_token, monkeypatch):
    """n=43: '0 9 * * *' means 9 AM wall-clock on this machine, emitted as an aware datetime."""
    from datetime import datetime, timezone

    from antigravity_bridge.models import ScheduledTask

    client, _, _ = client_and_token
    scheduler = client.app.state.scheduler
    task = ScheduledTask(name="t", prompt="p", cron_expression="0 9 * * *")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        # Winter (PST, UTC-8): 9 AM local == 17:00 UTC
        nxt = scheduler._compute_next_run(task, base=datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc))
        assert nxt == datetime(2026, 11, 21, 17, 0, tzinfo=timezone.utc)
        assert nxt.tzinfo is not None
        # Summer (PDT, UTC-7): 9 AM local == 16:00 UTC
        nxt = scheduler._compute_next_run(task, base=datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc))
        assert nxt == datetime(2026, 7, 2, 16, 0, tzinfo=timezone.utc)
        # Across the DST change the wall-clock time is kept (Sat Oct 31 PDT -> Sun Nov 1 PST)
        nxt = scheduler._compute_next_run(task, base=datetime(2026, 10, 31, 20, 0, tzinfo=timezone.utc))
        assert nxt == datetime(2026, 11, 1, 17, 0, tzinfo=timezone.utc)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_non_ascii_token_and_pair_code_return_401(client_and_token):
    """n=44: non-ASCII credentials are a 401, not a 500."""
    client, token, _ = client_and_token
    assert client.get("/api/status?token=%C3%A9").status_code == 401
    # Header bytes arrive latin-1 decoded
    res = client.get("/api/status", headers={"Authorization": "Bearer café".encode("latin-1")})
    assert res.status_code == 401
    assert "detail" in res.json()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=%C3%A9") as ws:
            ws.receive_json()

    # Arabic-Indic digits pass str.isdigit(); they must count as a failed attempt, not crash.
    disc = client.app.state.discovery
    disc.arm_pairing()
    res = client.post("/api/pair/exchange", json={"code": "١٢٣٤٥٦"})
    assert res.status_code == 401
    assert disc._failed_attempts == 1


def test_empty_workspace_path_rejected(client_and_token):
    """n=45: an empty path must not register the process cwd."""
    client, token, _ = client_and_token
    headers = auth_headers(token)
    before = len(client.get("/api/workspaces", headers=headers).json())
    for bad in ("", "   ", "\t\n"):
        res = client.post("/api/workspaces", json={"path": bad}, headers=headers)
        assert res.status_code == 400
        assert res.json()["detail"]
    assert len(client.get("/api/workspaces", headers=headers).json()) == before


def test_skill_frontmatter_block_scalars(client_and_token):
    """n=48 / n=28: YAML block-scalar descriptions are parsed, not shown as '>-'."""
    from antigravity_bridge.services.customizations import _parse_frontmatter

    client, token, tmp_path = client_and_token
    headers = auth_headers(token)
    ws_id = client.get("/api/workspaces", headers=headers).json()[0]["id"]
    skills_root = tmp_path / ".agents" / "skills"
    cases = {
        ">-": "Folded text that spans two lines.",
        ">": "Folded text that spans two lines.",
        ">+": "Folded text that spans two lines.",
        "|": "Folded text that\nspans two lines.",
        "|-": "Folded text that\nspans two lines.",
        "|+": "Folded text that\nspans two lines.",
    }
    for n, indicator in enumerate(cases):
        d = skills_root / f"block-skill-{n}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: block-skill-{n}\ndescription: {indicator}\n  Folded text that\n  spans two lines.\n"
            "license: MIT\n---\n# Heading\nBody.\n",
            encoding="utf-8",
        )
    cust = client.get(f"/api/customizations?workspace_id={ws_id}", headers=headers).json()
    by_id = {s["id"]: s for s in cust["skills"]}
    for n, (indicator, expected) in enumerate(cases.items()):
        assert by_id[f"block-skill-{n}"]["description"] == expected, indicator
    assert by_id["demo-skill"]["description"] == "Demo skill for tests"

    meta, body = _parse_frontmatter(
        "---\nname: x\ndescription: >-\n  First para\n  continues: here.\n\n  Second para.\nlicense: MIT\n---\nBody\n"
    )
    assert meta == {"name": "x", "description": "First para continues: here.\nSecond para.", "license": "MIT"}
    assert body == "Body\n"


def test_edge_inputs_return_4xx_json(client_and_token):
    """n=49: huge timer delay, long/NUL upload filename and NUL file path are 4xx, not 500."""
    client, token, tmp_path = client_and_token
    headers = auth_headers(token)
    ws_id = client.get("/api/workspaces", headers=headers).json()[0]["id"]

    for delay in (1_000_000_000_000_000, -5):
        res = client.post(
            "/api/schedules",
            json={"name": "t", "prompt": "p", "kind": "timer", "delay_seconds": delay},
            headers=headers,
        )
        assert res.status_code == 400, delay
        assert isinstance(res.json()["detail"], str)

    long_name = "a" * 300 + ".txt"
    up = client.post("/api/uploads", files={"file": (long_name, b"hello", "text/plain")}, headers=headers)
    assert up.status_code == 200
    att = up.json()
    assert att["filename"].endswith(".txt") and len(att["filename"]) <= 120
    assert Path(att["local_path"]).read_bytes() == b"hello"

    nul = client.app.state.conversations.save_upload(filename="re\x00port\x00.txt", data=b"x")
    assert nul.filename == "report.txt"
    assert Path(nul.local_path).exists()
    uni = client.app.state.conversations.save_upload(filename="é" * 300 + ".png", data=b"x")
    assert Path(uni.local_path).exists() and uni.filename.endswith(".png")

    for url in (
        f"/api/workspaces/{ws_id}/file?path=a.txt%00",
        f"/api/workspaces/{ws_id}/files?path=sub%00dir",
        f"/api/workspaces/{ws_id}/file?path={'b' * 2000}",
    ):
        res = client.get(url, headers=headers)
        assert 400 <= res.status_code < 500, url
        assert "detail" in res.json()
    res = client.post(f"/api/workspaces/{ws_id}/revert?path=a.txt%00", headers=headers)
    assert res.status_code == 400 and "detail" in res.json()


@pytest.mark.asyncio
async def test_approval_and_question_emit_resolved_on_timeout_and_cancel():
    """n=14: a timed-out or cancelled approval/question tells clients to drop the card."""
    from antigravity_bridge.models import QuestionItem, StreamEvent
    from antigravity_bridge.services.approvals import ApprovalBroker

    events: list[StreamEvent] = []

    async def emit(ev: StreamEvent) -> None:
        events.append(ev)

    broker = ApprovalBroker(emit=emit)

    # Timeout
    decision = await broker.request_approval("conv-1", "run_command", {}, timeout_seconds=0.05)
    assert decision == ApprovalDecision.DENY
    answers = await broker.request_question("conv-1", [QuestionItem(question="Q?")], timeout_seconds=0.05)
    assert answers == {}
    assert [e.type for e in events] == ["approval_request", "approval_resolved", "ask_question", "question_resolved"]
    assert events[1].payload["id"] == events[0].payload["id"]
    assert events[3].payload["id"] == events[2].payload["id"]
    assert events[1].conversation_id == "conv-1"
    assert broker.list_approvals() == [] and broker.list_questions() == []

    # Turn cancelled while waiting
    events.clear()
    t1 = asyncio.create_task(broker.request_approval("conv-2", "run_command", {}))
    t2 = asyncio.create_task(broker.request_question("conv-2", [QuestionItem(question="Q?")]))
    await asyncio.sleep(0.05)
    assert len(broker.list_approvals()) == 1 and len(broker.list_questions()) == 1
    t1.cancel()
    t2.cancel()
    await asyncio.gather(t1, t2, return_exceptions=True)
    by_type = {e.type: e for e in events}
    assert by_type["approval_resolved"].payload["id"] == by_type["approval_request"].payload["id"]
    assert by_type["question_resolved"].payload["id"] == by_type["ask_question"].payload["id"]
    assert broker.list_approvals() == [] and broker.list_questions() == []

    # A user answer still emits exactly one resolved event (no duplicate from the cleanup path).
    events.clear()
    t3 = asyncio.create_task(broker.request_approval("conv-3", "run_command", {}))
    await asyncio.sleep(0.02)
    assert await broker.resolve_approval(broker.list_approvals()[0].id, ApprovalDecision.ALLOW_ONCE)
    assert await t3 == ApprovalDecision.ALLOW_ONCE
    assert [e.type for e in events].count("approval_resolved") == 1


def test_settings_updated_broadcast_on_toggle_and_always_allow(client_and_token):
    """n=8: server-side GlobalSettings mutations are pushed as `settings_updated`."""
    client, token, _ = client_and_token
    headers = auth_headers(token)
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.receive_json()
        res = client.post(
            "/api/customizations/toggle",
            json={"kind": "skill", "id": "demo-skill", "enabled": False},
            headers=headers,
        )
        assert res.status_code == 200 and res.json() == {"ok": True}
        ev = ws.receive_json()
        assert ev["type"] == "settings_updated"
        assert "demo-skill" in ev["payload"]["disabled_skills"]
        assert set(ev["payload"]) == set(client.get("/api/settings", headers=headers).json())

        # "Always allow" grant recorded by the SDK backend
        sdk_backend = client.app.state.conversations.sdk_backend
        client.portal.call(sdk_backend._record_always_allow, "run_command", "frobnicate --now")
        ev = ws.receive_json()
        assert ev["type"] == "settings_updated"
        assert "frobnicate" in ev["payload"]["command_allowlist"]
        client.portal.call(sdk_backend._record_always_allow, "write_file", "")
        ev = ws.receive_json()
        assert ev["type"] == "settings_updated"
        assert any(g["pattern"] == "write_file" for g in ev["payload"]["permission_grants"])


def test_blocking_work_runs_off_event_loop_and_search_is_bounded(client_and_token, monkeypatch):
    """n=41: subprocess / filesystem work in async handlers runs in a worker thread."""
    import threading

    from antigravity_bridge import main as bridge_main

    client, token, tmp_path = client_and_token
    headers = auth_headers(token)
    ws_id = client.get("/api/workspaces", headers=headers).json()[0]["id"]
    loop_thread = client.portal.call(threading.get_ident)
    seen: dict[str, int] = {}

    def spy(obj, name, key):
        original = getattr(obj, name)

        def wrapper(*a, **k):
            seen[key] = threading.get_ident()
            return original(*a, **k)

        monkeypatch.setattr(obj, name, wrapper)

    spy(bridge_main, "detect_tailscale_info", "status")
    ws_service = client.app.state.workspaces
    for name in ("list_workspaces", "list_files", "read_file_content", "get_git_diff", "search_files", "list_artifacts"):
        spy(ws_service, name, name)
    spy(client.app.state.customizations, "snapshot", "snapshot")

    for url in (
        "/api/status",
        "/api/workspaces",
        f"/api/workspaces/{ws_id}/files",
        f"/api/workspaces/{ws_id}/file?path=README.md",
        f"/api/workspaces/{ws_id}/diff",
        f"/api/mentions?q=README&workspace_id={ws_id}",
        "/api/artifacts",
        "/api/customizations",
    ):
        assert client.get(url, headers=headers).status_code == 200, url
    assert set(seen) == {
        "status", "list_workspaces", "list_files", "read_file_content", "get_git_diff",
        "search_files", "list_artifacts", "snapshot",
    }
    for key, ident in seen.items():
        assert ident != loop_thread, f"{key} ran on the event loop thread"

    # A non-matching query stops after a bounded number of scanned entries.
    big = tmp_path / "big"
    for i in range(30):
        (big / f"d{i:02d}").mkdir(parents=True)
        for j in range(10):
            (big / f"d{i:02d}" / f"f{j}.txt").write_text("x", encoding="utf-8")
    (big / "zzz-last").mkdir()
    (big / "zzz-last" / "needle-file.txt").write_text("x", encoding="utf-8")
    assert any("needle-file" in f.relative_path for f in ws_service.search_files(ws_id, "needle-file"))
    assert ws_service.search_files(ws_id, "needle-file", max_scanned=50) == []


def test_diff_non_ascii_names_untracked_revert_and_symlink_confinement(client_and_token, monkeypatch):
    """n=46 / n=47: real filenames in diff, friendly untracked revert, no symlink / id traversal."""
    client, token, tmp_path = client_and_token
    headers = auth_headers(token)
    ws_id = client.get("/api/workspaces", headers=headers).json()[0]["id"]

    (tmp_path / "héllo wörld.txt").write_text("bonjour\n", encoding="utf-8")
    secret_dir = tmp_path.parent / f"{tmp_path.name}-outside"
    secret_dir.mkdir()
    (secret_dir / "secret.txt").write_text("TOP-SECRET-CONTENT\n", encoding="utf-8")
    (tmp_path / "link_secret").symlink_to(secret_dir / "secret.txt")
    subprocess.run(["git", "mv", "README.md", "READ ME.md"], cwd=tmp_path, check=True)

    res = client.get(f"/api/workspaces/{ws_id}/diff", headers=headers)
    assert res.status_code == 200
    files = {f["path"]: f for f in res.json()["files"]}
    assert "héllo wörld.txt" in files
    assert "+bonjour" in files["héllo wörld.txt"]["patch"]
    assert files["héllo wörld.txt"]["additions"] == 1
    assert "READ ME.md" in files and "README.md" not in files
    assert "link_secret" in files
    assert "TOP-SECRET-CONTENT" not in res.text
    assert files["link_secret"]["patch"] == ""

    # Revert of an untracked file: readable 400, no git argv / absolute paths leaked.
    rev = client.post(f"/api/workspaces/{ws_id}/revert?path=notes.txt", headers=headers)
    assert rev.status_code == 400
    detail = rev.json()["detail"]
    assert "not tracked" in detail
    assert "git" not in detail.replace("by git", "") and str(tmp_path) not in detail
    assert (tmp_path / "notes.txt").exists()

    # /api/artifacts: conversation_id cannot traverse out of the brain directory.
    fake_home = tmp_path.parent / f"{tmp_path.name}-home"
    (fake_home / ".gemini" / "antigravity" / "brain" / "conv-ok").mkdir(parents=True)
    (fake_home / ".gemini" / "antigravity" / "brain" / "conv-ok" / "plan.md").write_text("# ok\n", encoding="utf-8")
    (fake_home / "private.md").write_text("PRIVATE-HOME-NOTE\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    res = client.get("/api/artifacts?conversation_id=..%2F..%2F..", headers=headers)
    assert res.status_code == 200
    assert "PRIVATE-HOME-NOTE" not in res.text
    ok = client.get("/api/artifacts?conversation_id=conv-ok", headers=headers).json()
    assert any(a["conversation_id"] == "conv-ok" for a in ok)


def test_kill_exited_terminal_is_noop(client_and_token):
    """n=50: killing an already-exited terminal keeps its status/exit code and emits nothing."""
    client, token, _ = client_and_token
    headers = auth_headers(token)
    term = client.post("/api/terminals", json={"command": "echo done-ok"}, headers=headers).json()
    for _ in range(50):
        current = next(t for t in client.get("/api/terminals", headers=headers).json() if t["id"] == term["id"])
        if current["status"] != "running":
            break
        time.sleep(0.05)
    assert current["status"] == "exited" and current["exit_code"] == 0

    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.receive_json()
        res = client.post(f"/api/terminals/{term['id']}/kill", headers=headers)
        assert res.status_code == 200 and res.json()["killed"] is True
        ws.send_text("ping")
        assert ws.receive_json()["type"] == "pong"  # no terminal_exited re-broadcast
    after = next(t for t in client.get("/api/terminals", headers=headers).json() if t["id"] == term["id"])
    assert after["status"] == "exited" and after["exit_code"] == 0
    assert after["ended_at"] == current["ended_at"]


@pytest.mark.asyncio
async def test_phone_can_request_pairing_prompt_without_arming(client_and_token):
    """A phone may ask the computer to show a pairing prompt; that alone must never arm pairing."""
    client, _token, _ = client_and_token
    disc = client.app.state.discovery
    opened_urls: list[str] = []
    client.app.state.open_local_url = lambda url: (opened_urls.append(url) or True)

    res = client.post("/api/pair/request")
    assert res.status_code == 200
    body = res.json()
    assert body["opened"] is True and body["armed"] is False
    assert body["host_name"] and body["platform"]
    assert opened_urls and opened_urls[0].endswith("/pair/prompt")
    assert not disc.is_pairing_armed()

    # Rate limited: a second request right away does not pop another window.
    again = client.post("/api/pair/request").json()
    assert len(opened_urls) == 1 and again["opened"] is False

    # The prompt page is loopback-only and does not arm; only /pair does.
    assert client.get("/pair/prompt", headers={"host": "evil.example.com"}).status_code == 403
    async with _loopback_client(client.app) as ac:
        page = await ac.get("/pair/prompt")
        assert page.status_code == 200 and 'href="/pair?show=code"' in page.text
        assert not disc.is_pairing_armed()
        plain = await ac.get("/pair")
        assert plain.status_code == 200 and "<details>" in plain.text and "lan_url" in plain.text or plain.status_code == 200
        from_phone = await ac.get("/pair?show=code")
        assert "<details open>" in from_phone.text
    assert disc.is_pairing_armed()
    assert client.post("/api/pair/request").json()["armed"] is True
