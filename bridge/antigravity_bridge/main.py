"""FastAPI + WebSocket Pocket Relay Bridge for the Pocket Relay iOS app."""

from __future__ import annotations

import argparse
import asyncio
import copy
import html as html_lib
import io
import logging
import os
import platform as py_platform
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import qrcode
import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from . import __version__
from .auth import build_pairing_info, extract_token, render_qr_ascii, verify_bearer_token
from .config import DEFAULT_MODELS, BridgeStore, detect_tailscale_info
from .models import (
    ApprovalResponseRequest,
    CreateConversationRequest,
    CreateScheduledTaskRequest,
    CreateWorkspaceRequest,
    GlobalSettings,
    HostStatus,
    MentionRef,
    PairCodeExchangeRequest,
    PairingInfo,
    ProjectSettingsOverride,
    PushDeviceRequest,
    QuestionAnswerRequest,
    SearchResultItem,
    SendMessageRequest,
    StartTerminalRequest,
    StreamEvent,
    TerminalInputRequest,
    ToggleCustomizationRequest,
    UpdateScheduledTaskRequest,
)
from .runtime.acp_backend import AntigravityACPBackend
from .runtime.local_fallback import LocalFallbackBackend
from .runtime.sdk_backend import AntigravitySDKBackend
from .services.approvals import ApprovalBroker
from .services.conversations import ConversationService
from .services.customizations import CustomizationsService
from .services.discovery import DiscoveryService
from .services.push import PushService
from .services.tunnel import TunnelService
from .services.scheduler import SchedulerService
from .services.terminals import TerminalService
from .services.workspace import WorkspaceService


_TOKEN_IN_URL_RE = re.compile(r"(token=)[^&\s\"']+", flags=re.IGNORECASE)
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def redact_tokens(text: str) -> str:
    """Replace the value of any `token=` query parameter with REDACTED."""
    return _TOKEN_IN_URL_RE.sub(r"\1REDACTED", text)


class TokenRedactingFilter(logging.Filter):
    """Keeps bearer tokens passed as `?token=` out of uvicorn's access/error logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "token=" in record.msg.lower():
            record.msg = redact_tokens(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_tokens(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact_tokens(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return True


def install_log_redaction() -> None:
    """Attach the redaction filter to uvicorn's loggers (idempotent)."""
    for name in _UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, TokenRedactingFilter) for f in logger.filters):
            logger.addFilter(TokenRedactingFilter())


def build_uvicorn_log_config() -> dict[str, Any]:
    """uvicorn's default logging config plus token redaction on every handler."""
    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_tokens"] = {"()": TokenRedactingFilter}
    for handler in config.get("handlers", {}).values():
        handler.setdefault("filters", []).append("redact_tokens")
    return config


def is_loopback_host_header(host_header: str | None) -> bool:
    """True when a Host header names this machine's loopback (optional port).

    Rejecting other names defeats DNS rebinding against the loopback-only pairing page.
    """
    host = (host_header or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):
        name, sep, rest = host.partition("]")
        name += sep
        port = rest[1:] if rest.startswith(":") else rest
        if rest and not rest.startswith(":"):
            return False
    else:
        name, _, port = host.partition(":")
    if port and not port.isdigit():
        return False
    return name in ("127.0.0.1", "localhost", "[::1]")


def open_url_on_this_computer(url: str) -> bool:
    """Open `url` in the default browser of the logged-in desktop session. Returns False if it could not."""
    if os.environ.get("ANTIGRAVITY_DISABLE_BONJOUR", "").strip().lower() in ("1", "true", "yes"):
        return False  # headless / test mode: never pop windows
    import subprocess

    try:
        system = py_platform.system()
        if system == "Darwin":
            subprocess.Popen(["/usr/bin/open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif system == "Windows":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            import webbrowser

            return bool(webbrowser.open(url))
        return True
    except Exception:
        return False


PAIR_PROMPT_MIN_INTERVAL_SECONDS = 15.0


class SocketHub:
    """Broadcasts StreamEvents to all authenticated iOS WebSocket clients."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: Any) -> None:
        self._clients.discard(ws)

    def add_listener(self, listener: Any) -> None:
        """Adds anything with an async `send_text`, such as a remote-access tunnel session."""
        self._clients.add(listener)

    async def broadcast(self, event: StreamEvent) -> None:
        if not self._clients:
            return
        payload = event.model_dump_json()
        stale: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self._clients.discard(ws)


def create_app(
    data_dir: Path | None = None,
    port: int = 8765,
    public_url: str | None = None,
) -> FastAPI:
    started_at = time.monotonic()
    store = BridgeStore(data_dir=data_dir)
    hub = SocketHub()
    push = PushService(
        data_dir=store.data_dir,
        host_name=lambda: store.host_name,
        conversation_title=lambda cid: getattr(store.conversations.get(cid), "title", None),
        waiting_count=lambda: len(approvals.list_approvals()) + len(approvals.list_questions()),
    )
    # Approvals, questions and finished turns also go out as push alerts to registered phones.
    emit_with_push = push.wrap(hub.broadcast)
    approvals = ApprovalBroker(emit=emit_with_push)
    workspace_service = WorkspaceService(store)
    customizations = CustomizationsService(store)
    terminals = TerminalService(store, emit=hub.broadcast)
    sdk_backend = AntigravitySDKBackend(store, approvals, customizations)
    acp_backend = AntigravityACPBackend(approvals=approvals)
    fallback_backend = LocalFallbackBackend(approvals, workspace_service)
    conversations = ConversationService(
        store=store,
        workspace_service=workspace_service,
        customizations=customizations,
        sdk_backend=sdk_backend,
        acp_backend=acp_backend,
        fallback_backend=fallback_backend,
        emit=emit_with_push,
    )
    scheduler = SchedulerService(store, conversations, emit=hub.broadcast)
    discovery = DiscoveryService(store, port=port, public_url=public_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        scheduler.start()
        tunnel.start()
        await discovery.start()
        if acp_backend.is_available():
            try:
                models = await acp_backend.probe_models()
                if models and store.settings.model not in models:
                    store.settings.model = models[0]
                    store.save()
            except Exception:
                pass
        try:
            yield
        finally:
            await push.drain()
            await tunnel.stop()
            await discovery.stop()
            await scheduler.stop()
            await terminals.shutdown()

    app = FastAPI(title="Pocket Relay Bridge", version=__version__, lifespan=lifespan)
    # No CORS middleware on purpose: the native iOS app does not need it, and allowing
    # browser origins would let any web page read the pairing page / call the API.
    install_log_redaction()

    def token_is_valid(token: str | None) -> bool:
        try:
            verify_bearer_token(store, token)
            return True
        except HTTPException:
            return False

    def connected_event_json() -> str:
        return StreamEvent(
            type="connected",
            payload={
                "host_id": store.host_id,
                "host_name": store.host_name,
                "pending_approvals": [a.model_dump(mode="json") for a in approvals.list_approvals()],
                "pending_questions": [q.model_dump(mode="json") for q in approvals.list_questions()],
            },
        ).model_dump_json()

    # Remote access: an outbound, end-to-end encrypted tunnel for phones on another network.
    tunnel = TunnelService(
        data_dir=store.data_dir,
        app=app,
        hub=hub,
        verify_token=token_is_valid,
        connected_event=connected_event_json,
    )

    def remote_info() -> dict[str, str] | None:
        if not tunnel.enabled:
            return None
        return {
            "relay_url": tunnel.relay_url,
            "relay_room": tunnel.credentials.room_id,
            "relay_key": tunnel.credentials.tunnel_key_text,
        }

    async def broadcast_settings() -> None:
        await hub.broadcast(
            StreamEvent(type="settings_updated", payload=store.settings.model_dump(mode="json"))
        )

    sdk_backend.on_settings_changed = broadcast_settings

    # Expose internals for tests
    app.state.store = store
    app.state.hub = hub
    app.state.approvals = approvals
    app.state.workspaces = workspace_service
    app.state.customizations = customizations
    app.state.terminals = terminals
    app.state.conversations = conversations
    app.state.scheduler = scheduler
    app.state.discovery = discovery
    app.state.push = push
    app.state.tunnel = tunnel

    def require_auth(token: str | None = Depends(extract_token)) -> None:
        verify_bearer_token(store, token)

    # ------------------------------------------------------------------
    # Health, Status & Pairing
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "host_name": store.host_name}

    @app.get("/api/status", response_model=HostStatus, dependencies=[Depends(require_auth)])
    async def get_status() -> HostStatus:
        dns, ip = await asyncio.to_thread(detect_tailscale_info)
        sys_name = py_platform.system()
        pretty_os = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(sys_name, sys_name)
        return HostStatus(
            host_id=store.host_id,
            host_name=store.host_name,
            version=__version__,
            platform=pretty_os,
            tailscale_dns=dns,
            tailscale_ip=ip,
            active_backend=conversations.active_backend_name(),
            sdk_available=sdk_backend.is_available(),
            sdk_authenticated=sdk_backend.is_authenticated(),
            acp_available=acp_backend.is_available(),
            available_models=acp_backend.discovered_models or DEFAULT_MODELS,
            active_conversations=len(store.conversations),
            running_tasks=terminals.running_count(),
            pending_approvals=len(approvals.list_approvals()),
            uptime_seconds=round(time.monotonic() - started_at, 1),
        )

    @app.get("/api/pairing", response_model=PairingInfo, dependencies=[Depends(require_auth)])
    async def get_pairing() -> PairingInfo:
        return await asyncio.to_thread(build_pairing_info, store, port=port, public_url=public_url, remote=remote_info())

    @app.post("/api/pairing/rotate", response_model=PairingInfo, dependencies=[Depends(require_auth)])
    async def rotate_pairing() -> PairingInfo:
        store.rotate_token()
        push.clear()
        # Unpairing every phone also has to cut off remote access for them.
        tunnel.credentials.rotate()
        if tunnel.enabled:
            await tunnel.restart()
        return await asyncio.to_thread(build_pairing_info, store, port=port, public_url=public_url, remote=remote_info())

    @app.post("/api/push/devices", dependencies=[Depends(require_auth)])
    async def register_push_device(req: PushDeviceRequest) -> dict[str, Any]:
        if not PushService.is_valid_token(req.device_token):
            raise HTTPException(status_code=422, detail="Invalid device token")
        if req.environment not in {"sandbox", "production"}:
            raise HTTPException(status_code=422, detail="Invalid environment")
        if not await push.service_ready():
            # The phone keeps its own alerts until the push service can deliver.
            raise HTTPException(status_code=503, detail="Push alerts are not available yet")
        push.register(req.device_token, req.environment, req.show_detail)
        return {"registered": True, "enabled": push.enabled}

    @app.delete("/api/push/devices/{device_token}", dependencies=[Depends(require_auth)])
    async def unregister_push_device(device_token: str) -> dict[str, Any]:
        return {"removed": push.unregister(device_token)}

    @app.get("/api/pairing/qr.png", dependencies=[Depends(require_auth)])
    async def get_pairing_qr_png() -> Response:
        info = await asyncio.to_thread(build_pairing_info, store, port=port, public_url=public_url, remote=remote_info())
        img = qrcode.make(info.pairing_uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    @app.post("/api/pair/exchange", response_model=PairingInfo)
    async def exchange_pair_code(req: PairCodeExchangeRequest) -> PairingInfo:
        if not discovery.is_pairing_armed():
            raise HTTPException(
                status_code=403,
                detail=(
                    "No pairing code is showing on that computer yet. On the computer, open the pairing page "
                    f"(Pocket Relay Bridge menu bar icon, or http://127.0.0.1:{port}/pair), "
                    "then enter the 6-digit code shown there."
                ),
            )
        if discovery.is_rate_limited():
            raise HTTPException(
                status_code=429,
                detail="Too many failed pairing attempts. Try again in 60 seconds.",
            )
        info = discovery.exchange_pair_code(req.code)
        if not info:
            raise HTTPException(status_code=401, detail="Invalid or expired 6-digit pairing code.")
        return info

    app.state.open_local_url = open_url_on_this_computer
    app.state.last_pair_prompt_at = 0.0

    @app.post("/api/pair/request")
    async def request_pairing_prompt() -> dict[str, Any]:
        """A phone on the network asks this computer to show its pairing window.

        Unauthenticated on purpose (the phone has no token yet), so it must stay harmless:
        it only opens a local page that asks the person at this computer to click
        "Show pairing code". It never arms pairing or reveals the code by itself.
        """
        now = time.monotonic()
        opened = False
        if now - app.state.last_pair_prompt_at >= PAIR_PROMPT_MIN_INTERVAL_SECONDS:
            app.state.last_pair_prompt_at = now
            opened = bool(
                await asyncio.to_thread(app.state.open_local_url, f"http://127.0.0.1:{port}/pair/prompt")
            )
        elif discovery.is_pairing_armed():
            opened = True  # a pairing window is already showing
        sys_name = py_platform.system()
        return {
            "opened": opened,
            "armed": discovery.is_pairing_armed(),
            "host_name": store.host_name,
            "platform": {"Darwin": "macOS"}.get(sys_name, sys_name),
        }

    @app.get("/pair/prompt", response_class=HTMLResponse)
    async def local_pair_prompt_page(request: Request) -> HTMLResponse:
        client_host = request.client.host if request.client else ""
        if client_host not in ("127.0.0.1", "::1", "localhost") or not is_loopback_host_header(
            request.headers.get("host")
        ):
            raise HTTPException(status_code=403, detail="This page only opens on the computer itself.")
        html = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pair your iPhone - Pocket Relay Bridge</title>
<style>
  body { background:#0b0d17; color:#fff; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }
  .card { background:#151828; border:1px solid rgba(255,255,255,0.1); border-radius:24px; padding:36px;
          max-width:440px; text-align:center; box-shadow:0 20px 40px rgba(0,0,0,0.5); }
  h1 { font-size:24px; margin:0 0 10px; }
  p { color:#a0a5c0; font-size:15px; line-height:1.5; margin:0 0 22px; }
  a.btn { display:block; background:linear-gradient(135deg,#6366f1,#8b5cf6,#06b6d4); color:#fff; text-decoration:none;
          padding:15px; border-radius:14px; font-weight:700; font-size:17px; }
  .small { font-size:13px; margin:18px 0 0; }
</style></head>
<body><div class="card">
  <h1>Pair an iPhone or iPad?</h1>
  <p>A device nearby running <strong>Pocket Relay</strong> wants to connect to this computer.
     If that is you, show the pairing code and type it on your phone.</p>
  <a class="btn" href="/pair?show=code">Show pairing code</a>
  <p class="small">Not you? Just close this tab. Nothing is shared until a code is shown and entered.</p>
</div></body></html>"""
        return HTMLResponse(content=html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    @app.get("/pair", response_class=HTMLResponse)
    async def local_pair_page(request: Request) -> HTMLResponse:
        client_host = request.client.host if request.client else ""
        denied = HTTPException(
            status_code=403,
            detail=f"Open http://127.0.0.1:{port}/pair directly on your computer to view the pairing QR & code.",
        )
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            raise denied
        # DNS-rebinding guard: the page embeds the bearer token, so the browser must be
        # addressing this machine by a loopback name, not an attacker-controlled hostname.
        if not is_loopback_host_header(request.headers.get("host")):
            raise denied
        # Only a page the user actually navigated to may show (and arm) pairing; refuse
        # cross-site subresource/fetch/iframe requests issued by other web pages.
        fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
        if fetch_site in ("cross-site", "same-site") and not (
            (request.headers.get("sec-fetch-mode") or "").lower() == "navigate"
            and (request.headers.get("sec-fetch-dest") or "").lower() == "document"
        ):
            raise denied
        import base64 as _b64

        info = await asyncio.to_thread(build_pairing_info, store, port=port, public_url=public_url, remote=remote_info())
        code, remaining = discovery.arm_pairing()
        # Arriving from the phone's "type a code" flow: show the code without an extra click.
        details_open = " open" if request.query_params.get("show") == "code" else ""
        formatted_code = f"{code[:3]} {code[3:]}"
        img = qrcode.make(info.pairing_uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = _b64.b64encode(buf.getvalue()).decode("ascii")
        sys_name = py_platform.system()
        pretty_os = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(sys_name, sys_name)
        backend_name = conversations.active_backend_name()
        model_count = len(acp_backend.discovered_models or DEFAULT_MODELS)
        safe_host = html_lib.escape(info.host_name)
        safe_base_url = html_lib.escape(info.base_url)
        safe_pairing_uri = html_lib.escape(info.pairing_uri, quote=True)

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Pair Pocket Relay</title>
<style>
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#0b0c14; color:#f4f6fb; display:flex; align-items:center; justify-content:center; min-height:100vh; padding:24px; }}
  .steps {{ text-align:left; margin:14px 0 18px; }}
  .step {{ display:flex; gap:12px; align-items:flex-start; margin:10px 0; font-size:16px; line-height:1.4; color:#e6e8f5; }}
  .num {{ flex:0 0 26px; height:26px; border-radius:13px; background:linear-gradient(135deg,#6366f1,#8b5cf6,#06b6d4);
          display:inline-flex; align-items:center; justify-content:center; font-weight:700; font-size:14px; }}
  .hint {{ font-size:13px; }}
  details {{ margin-top:16px; text-align:center; }}
  summary {{ cursor:pointer; color:#a5b4fc; font-size:14px; font-weight:600; }}
  .card {{ background:#151828; border:1px solid rgba(255,255,255,0.1); border-radius:24px; max-width:440px; width:100%; padding:32px; text-align:center; box-shadow:0 24px 60px rgba(0,0,0,0.45); }}
  .pill {{ display:inline-block; padding:5px 12px; border-radius:999px; font-size:12px; font-weight:600; background:rgba(99,102,241,0.18); color:#a5b4fc; margin-bottom:14px; }}
  h1 {{ margin:0 0 8px; font-size:24px; }}
  p {{ margin:0 0 20px; color:#9ca3af; font-size:14px; line-height:1.5; }}
  .qr {{ background:#fff; padding:16px; border-radius:18px; display:inline-block; margin-bottom:20px; }}
  .qr img {{ width:220px; height:220px; display:block; }}
  .code-box {{ background:#0d0f1a; border:1px solid rgba(6,182,212,0.35); border-radius:14px; padding:14px; margin-bottom:18px; }}
  .code-label {{ font-size:11px; text-transform:uppercase; letter-spacing:1.2px; color:#67e8f9; margin-bottom:6px; }}
  .code {{ font-size:34px; font-weight:800; letter-spacing:6px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; color:#fff; }}
  .meta {{ font-size:12px; color:#94a3b8; margin-top:12px; }}
  button {{ width:100%; border:0; border-radius:12px; padding:12px; font-size:14px; font-weight:600; cursor:pointer; background:linear-gradient(135deg,#6366f1,#8b5cf6,#06b6d4); color:#fff; }}
</style>
</head>
<body>
  <div class="card">
    <div class="pill">{pretty_os} • {backend_name} • {model_count} models</div>
    <h1>Pair your iPhone with {safe_host}</h1>
    <div class="steps">
      <div class="step"><span class="num">1</span><span>Open the <strong>Camera</strong> app on your iPhone or iPad and point it at this code.</span></div>
      <div class="step"><span class="num">2</span><span>Tap the yellow <strong>Pocket Relay</strong> link that appears. The app opens and connects by itself.</span></div>
    </div>
    <div class="qr"><img src="data:image/png;base64,{qr_b64}" alt="Pairing QR code" /></div>
    <p class="hint">Don't have the app yet? Install <strong>Pocket Relay</strong> from TestFlight first, then scan.</p>
    <details{details_open}>
      <summary>Can't scan? Use a 6-digit code instead</summary>
      <p>In the app, tap this computer under <em>Or pick a nearby computer</em> and type:</p>
      <div class="code-box">
        <div class="code-label">Pairing code (valid {max(1, remaining // 60)} min)</div>
        <div class="code">{formatted_code}</div>
      </div>
      <button onclick="navigator.clipboard.writeText('{safe_pairing_uri}'); this.innerText='Copied pairing link';">Copy pairing link instead</button>
    </details>
    <div class="meta">{safe_base_url}</div>
  </div>
</body>
</html>"""
        return HTMLResponse(
            content=html,
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
            },
        )

    # ------------------------------------------------------------------
    # Workspaces / Projects, Files, Git Diffs, Artifacts
    # ------------------------------------------------------------------

    @app.get("/api/workspaces", dependencies=[Depends(require_auth)])
    async def list_workspaces():
        return await asyncio.to_thread(workspace_service.list_workspaces)

    @app.post("/api/workspaces", dependencies=[Depends(require_auth)])
    async def create_workspace(req: CreateWorkspaceRequest):
        try:
            return workspace_service.add_workspace(req.path, name=req.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.delete("/api/workspaces/{workspace_id}", dependencies=[Depends(require_auth)])
    async def delete_workspace(workspace_id: str):
        if not workspace_service.remove_workspace(workspace_id):
            raise HTTPException(status_code=404, detail="Workspace not found")
        return {"deleted": True}

    @app.put("/api/workspaces/{workspace_id}/settings", dependencies=[Depends(require_auth)])
    async def update_workspace_settings(workspace_id: str, override: ProjectSettingsOverride):
        try:
            return workspace_service.update_workspace_settings(workspace_id, override)
        except KeyError:
            raise HTTPException(status_code=404, detail="Workspace not found")

    @app.get("/api/workspaces/{workspace_id}/files", dependencies=[Depends(require_auth)])
    async def list_workspace_files(workspace_id: str, path: str = Query(default="")):
        try:
            return await asyncio.to_thread(workspace_service.list_files, workspace_id, path)
        except KeyError:
            raise HTTPException(status_code=404, detail="Workspace not found")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except (ValueError, OSError):
            raise HTTPException(status_code=400, detail="Invalid path.")

    @app.get("/api/workspaces/{workspace_id}/file", dependencies=[Depends(require_auth)])
    async def read_workspace_file(workspace_id: str, path: str = Query(...)):
        try:
            content = await asyncio.to_thread(workspace_service.read_file_content, workspace_id, path)
            return {"path": path, "content": content}
        except KeyError:
            raise HTTPException(status_code=404, detail="Workspace not found")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="File not found")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except (ValueError, OSError):
            raise HTTPException(status_code=400, detail="Invalid path.")

    @app.get("/api/workspaces/{workspace_id}/diff", dependencies=[Depends(require_auth)])
    async def get_workspace_diff(workspace_id: str):
        try:
            return await asyncio.to_thread(workspace_service.get_git_diff, workspace_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Workspace not found")

    @app.post("/api/workspaces/{workspace_id}/revert", dependencies=[Depends(require_auth)])
    async def revert_workspace_file(workspace_id: str, path: str = Query(...)):
        try:
            await asyncio.to_thread(workspace_service.revert_file, workspace_id, path)
            return {"reverted": True, "path": path}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0]) if exc.args else "Not found")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception:
            raise HTTPException(status_code=400, detail="Could not revert this file.")

    @app.get("/api/artifacts", dependencies=[Depends(require_auth)])
    async def list_artifacts(
        conversation_id: str | None = Query(default=None),
        workspace_id: str | None = Query(default=None),
    ):
        return await asyncio.to_thread(
            workspace_service.list_artifacts, conversation_id=conversation_id, workspace_id=workspace_id
        )

    # ------------------------------------------------------------------
    # @Mentions & Global Search / Command Palette
    # ------------------------------------------------------------------

    @app.get("/api/mentions", response_model=list[MentionRef], dependencies=[Depends(require_auth)])
    async def search_mentions(
        q: str = Query(default=""),
        workspace_id: str | None = Query(default=None),
    ) -> list[MentionRef]:
        results: list[MentionRef] = []
        q_low = q.strip().lower()

        if workspace_id:
            for f in await asyncio.to_thread(workspace_service.search_files, workspace_id, q, limit=20):
                results.append(
                    MentionRef(
                        kind="folder" if f.is_directory else "file",
                        id=f.relative_path,
                        label=f.relative_path,
                        detail="Folder" if f.is_directory else f"{f.size_bytes} B",
                    )
                )

        for conv in conversations.list_conversations()[:10]:
            if not q_low or q_low in conv.title.lower():
                results.append(
                    MentionRef(
                        kind="conversation",
                        id=conv.id,
                        label=conv.title,
                        detail=f"{conv.message_count} messages",
                    )
                )

        snap = await asyncio.to_thread(customizations.snapshot, workspace_id=workspace_id)
        for rule in snap.rules:
            if not q_low or q_low in rule.name.lower():
                results.append(MentionRef(kind="rule", id=rule.id, label=rule.name, detail=rule.scope))
        for mcp in snap.mcp_servers:
            if not q_low or q_low in mcp.name.lower():
                results.append(MentionRef(kind="mcp", id=mcp.id, label=mcp.name, detail=mcp.transport))
        return results[:40]

    @app.get("/api/search", response_model=list[SearchResultItem], dependencies=[Depends(require_auth)])
    async def global_search(
        q: str = Query(default=""),
        workspace_id: str | None = Query(default=None),
    ) -> list[SearchResultItem]:
        ql = q.strip().lower()
        hits: list[SearchResultItem] = []

        for conv in conversations.list_conversations():
            if not ql or ql in conv.title.lower() or ql in conv.last_message_preview.lower():
                hits.append(
                    SearchResultItem(
                        kind="conversation",
                        id=conv.id,
                        title=conv.title,
                        subtitle=conv.last_message_preview or conv.model,
                        workspace_id=conv.workspace_id,
                    )
                )

        for ws in await asyncio.to_thread(workspace_service.list_workspaces):
            if not ql or ql in ws.name.lower() or ql in ws.path.lower():
                hits.append(
                    SearchResultItem(
                        kind="workspace",
                        id=ws.id,
                        title=ws.name,
                        subtitle=ws.path,
                        workspace_id=ws.id,
                    )
                )

        snap = await asyncio.to_thread(customizations.snapshot, workspace_id=workspace_id)
        for cmd in snap.slash_commands:
            if not ql or ql in cmd.name.lower() or ql in cmd.description.lower():
                hits.append(SearchResultItem(kind="command", id=cmd.name, title=cmd.name, subtitle=cmd.description))
        for skill in snap.skills:
            if not ql or ql in skill.name.lower() or ql in skill.description.lower():
                hits.append(SearchResultItem(kind="skill", id=skill.id, title=skill.name, subtitle=skill.description))

        if workspace_id and ql:
            for f in await asyncio.to_thread(workspace_service.search_files, workspace_id, ql, limit=15):
                hits.append(
                    SearchResultItem(
                        kind="file",
                        id=f.relative_path,
                        title=f.name,
                        subtitle=f.relative_path,
                        workspace_id=workspace_id,
                    )
                )
        return hits[:50]

    # ------------------------------------------------------------------
    # Conversations, Messages, Uploads, Subagents
    # ------------------------------------------------------------------

    @app.get("/api/conversations", dependencies=[Depends(require_auth)])
    async def list_conversations(workspace_id: str | None = Query(default=None)):
        return conversations.list_conversations(workspace_id=workspace_id)

    @app.post("/api/conversations", dependencies=[Depends(require_auth)])
    async def create_conversation(req: CreateConversationRequest):
        return conversations.create_conversation(
            title=req.title, workspace_id=req.workspace_id, model=req.model
        )

    @app.get("/api/conversations/{conversation_id}", dependencies=[Depends(require_auth)])
    async def get_conversation(conversation_id: str):
        conv = conversations.get_conversation(conversation_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return conv

    @app.delete("/api/conversations/{conversation_id}", dependencies=[Depends(require_auth)])
    async def delete_conversation(conversation_id: str):
        if not conversations.delete_conversation(conversation_id):
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"deleted": True}

    @app.post("/api/conversations/{conversation_id}/messages", dependencies=[Depends(require_auth)])
    async def send_message(conversation_id: str, req: SendMessageRequest):
        try:
            return await conversations.send_message(
                conversation_id=conversation_id,
                content=req.content,
                mentions=req.mentions,
                attachment_ids=req.attachment_ids,
                slash_command=req.slash_command,
                model=req.model,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Conversation not found")

    @app.post("/api/conversations/{conversation_id}/cancel", dependencies=[Depends(require_auth)])
    async def cancel_turn(conversation_id: str):
        cancelled = await conversations.cancel_turn(conversation_id)
        return {"cancelled": cancelled}

    @app.post("/api/conversations/{conversation_id}/compact", dependencies=[Depends(require_auth)])
    async def compact_conversation(conversation_id: str):
        try:
            return await conversations.compact_conversation(conversation_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Conversation not found")

    @app.post("/api/conversations/{conversation_id}/pin", dependencies=[Depends(require_auth)])
    async def pin_conversation(conversation_id: str, pinned: bool = Query(default=True)):
        try:
            return conversations.toggle_pin(conversation_id, pinned)
        except KeyError:
            raise HTTPException(status_code=404, detail="Conversation not found")

    @app.post("/api/uploads", dependencies=[Depends(require_auth)])
    async def upload_file(file: UploadFile = File(...)):
        data = await file.read()
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Attachment exceeds 25 MB limit.")
        try:
            return conversations.save_upload(
                filename=file.filename or "attachment",
                data=data,
                mime_type=file.content_type,
            )
        except (ValueError, OSError):
            raise HTTPException(status_code=400, detail="Could not store attachment: invalid file name.")

    @app.get("/api/subagents", dependencies=[Depends(require_auth)])
    async def list_subagents(conversation_id: str | None = Query(default=None)):
        return conversations.list_subagents(conversation_id=conversation_id)

    @app.post("/api/subagents/{subagent_id}/kill", dependencies=[Depends(require_auth)])
    async def kill_subagent(subagent_id: str):
        ok = await conversations.kill_subagent(subagent_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Subagent not found")
        return {"killed": True}

    # ------------------------------------------------------------------
    # Approvals & Interactive Questions
    # ------------------------------------------------------------------

    @app.get("/api/approvals", dependencies=[Depends(require_auth)])
    async def list_approvals():
        return approvals.list_approvals()

    @app.post("/api/approvals/{approval_id}", dependencies=[Depends(require_auth)])
    async def resolve_approval(approval_id: str, req: ApprovalResponseRequest):
        ok = await approvals.resolve_approval(approval_id, req.decision)
        if not ok:
            raise HTTPException(status_code=404, detail="Pending approval not found")
        return {"resolved": True, "decision": req.decision}

    @app.get("/api/questions", dependencies=[Depends(require_auth)])
    async def list_questions():
        return approvals.list_questions()

    @app.post("/api/questions/{question_id}", dependencies=[Depends(require_auth)])
    async def resolve_question(question_id: str, req: QuestionAnswerRequest):
        ok = await approvals.resolve_question(question_id, req.answers)
        if not ok:
            raise HTTPException(status_code=404, detail="Pending question not found")
        return {"resolved": True}

    # ------------------------------------------------------------------
    # Terminals & Background Tasks
    # ------------------------------------------------------------------

    @app.get("/api/terminals", dependencies=[Depends(require_auth)])
    async def list_terminals():
        return terminals.list_sessions()

    @app.post("/api/terminals", dependencies=[Depends(require_auth)])
    async def start_terminal(req: StartTerminalRequest):
        try:
            return await terminals.start(req.command, workspace_id=req.workspace_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

    @app.post("/api/terminals/{session_id}/input", dependencies=[Depends(require_auth)])
    async def terminal_input(session_id: str, req: TerminalInputRequest):
        ok = await terminals.send_input(session_id, req.text)
        if not ok:
            raise HTTPException(status_code=400, detail="Terminal is not running")
        return {"sent": True}

    @app.post("/api/terminals/{session_id}/kill", dependencies=[Depends(require_auth)])
    async def kill_terminal(session_id: str):
        ok = await terminals.kill(session_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Terminal not found")
        return {"killed": True}

    # ------------------------------------------------------------------
    # Customizations: Skills, Rules, MCP Servers, Plugins, Slash Commands
    # ------------------------------------------------------------------

    @app.get("/api/customizations", dependencies=[Depends(require_auth)])
    async def get_customizations(workspace_id: str | None = Query(default=None)):
        return await asyncio.to_thread(customizations.snapshot, workspace_id=workspace_id, include_content=False)

    @app.get("/api/customizations/skills/{skill_id}", dependencies=[Depends(require_auth)])
    async def get_skill_detail(skill_id: str, workspace_id: str | None = Query(default=None)):
        item = await asyncio.to_thread(customizations.get_skill, skill_id, workspace_id=workspace_id)
        if not item:
            raise HTTPException(status_code=404, detail="Skill not found")
        return item

    @app.post("/api/customizations/toggle", dependencies=[Depends(require_auth)])
    async def toggle_customization(req: ToggleCustomizationRequest):
        try:
            customizations.toggle(req.kind, req.id, req.enabled)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await broadcast_settings()
        return {"ok": True}

    # ------------------------------------------------------------------
    # Scheduled Tasks
    # ------------------------------------------------------------------

    @app.get("/api/schedules", dependencies=[Depends(require_auth)])
    async def list_schedules():
        return scheduler.list_tasks()

    @app.post("/api/schedules", dependencies=[Depends(require_auth)])
    async def create_schedule(req: CreateScheduledTaskRequest):
        try:
            return scheduler.create_task(req)
        except (ValueError, OverflowError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.patch("/api/schedules/{task_id}", dependencies=[Depends(require_auth)])
    async def update_schedule(task_id: str, req: UpdateScheduledTaskRequest):
        try:
            return scheduler.update_task(task_id, req)
        except KeyError:
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.delete("/api/schedules/{task_id}", dependencies=[Depends(require_auth)])
    async def delete_schedule(task_id: str):
        if not scheduler.delete_task(task_id):
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        return {"deleted": True}

    @app.post("/api/schedules/{task_id}/run", dependencies=[Depends(require_auth)])
    async def run_schedule_now(task_id: str):
        try:
            return await scheduler.run_now(task_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Scheduled task not found")

    # ------------------------------------------------------------------
    # Global Settings
    # ------------------------------------------------------------------

    @app.get("/api/settings", response_model=GlobalSettings, dependencies=[Depends(require_auth)])
    async def get_settings() -> GlobalSettings:
        return store.settings

    @app.put("/api/settings", response_model=GlobalSettings, dependencies=[Depends(require_auth)])
    async def update_settings(new_settings: GlobalSettings) -> GlobalSettings:
        store.settings = new_settings
        store.save()
        await broadcast_settings()
        return store.settings

    # ------------------------------------------------------------------
    # Real-Time WebSocket Stream
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket, token: str | None = Query(default=None)):
        # Preferred: `Authorization: Bearer <token>` on the handshake (keeps the token out
        # of URLs and logs). `?token=` is still accepted for existing clients.
        authorization = ws.headers.get("authorization")
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        try:
            verify_bearer_token(store, token)
        except HTTPException:
            await ws.close(code=4401)
            return

        await hub.connect(ws)
        try:
            await ws.send_text(connected_event_json())
            while True:
                msg = await ws.receive_text()
                if msg == "ping":
                    await ws.send_text(StreamEvent(type="pong").model_dump_json())
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(ws)

    return app


def cli_main() -> None:
    parser = argparse.ArgumentParser(description="Pocket Relay Bridge")
    parser.add_argument("--host", default=os.environ.get("ANTIGRAVITY_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ANTIGRAVITY_BRIDGE_PORT", "8765")))
    parser.add_argument("--print-qr", action="store_true", help="Print pairing QR code on startup")
    parser.add_argument("--public-url", default=None, help="Override public URL encoded into QR")
    args = parser.parse_args()

    app = create_app(port=args.port, public_url=args.public_url)
    store: BridgeStore = app.state.store
    tunnel = app.state.tunnel
    remote = (
        {"relay_url": tunnel.relay_url, "relay_room": tunnel.credentials.room_id,
         "relay_key": tunnel.credentials.tunnel_key_text}
        if tunnel.enabled else None
    )
    info = build_pairing_info(store, port=args.port, public_url=args.public_url, remote=remote)

    print("\n=== Pocket Relay Bridge ===")
    print(f"Host:       {info.host_name} ({info.host_id})")
    print(f"Local URL:  {info.local_url}")
    if info.tailscale_url:
        print(f"Tailscale:  {info.tailscale_url}")
    # Secrets only go to an interactive terminal, never to a log file (launchd/systemd).
    interactive = sys.stdout.isatty()
    if interactive:
        print(f"Token:      {info.token}")
        print(f"Pair URI:   {info.pairing_uri}")
    else:
        print(f"Pairing:    open http://127.0.0.1:{args.port}/pair on this computer")
    if args.print_qr and interactive:
        print("\nScan from Pocket Relay iOS → Hosts → Pair via QR:\n")
        print(render_qr_ascii(info.pairing_uri))
    print("===========================================\n", flush=True)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        log_config=build_uvicorn_log_config(),
    )


if __name__ == "__main__":
    cli_main()
