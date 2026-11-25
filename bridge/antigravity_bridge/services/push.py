"""Push alerts: tells paired phones when the agent needs the user or has finished.

Apple only delivers push notifications sent with the app developer's signing key, which cannot be
shipped inside this open-source bridge. So the bridge sends a small request to the Pocket Relay
push relay, which signs it and hands it to Apple. By default the request says only what kind of
event happened and this computer's name. The command or question text is included only for phones
whose owner switched on "Show Details in Alerts".

Set POCKET_RELAY_PUSH_URL to use a relay you run yourself, or POCKET_RELAY_PUSH=0 to turn push off.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from ..models import StreamEvent

logger = logging.getLogger("antigravity_bridge.push")

DEFAULT_RELAY_URL = "https://push.pocketrelay.app/v1/notify"
MAX_DEVICES = 10
MAX_DETAIL_CHARS = 180
_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{64,200}$")

Emit = Callable[[StreamEvent], Awaitable[None]]
Sender = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def _http_sender(url: str, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.post(url, json=body)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            return {}


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:MAX_DETAIL_CHARS]
    return ""


class PushService:
    def __init__(
        self,
        data_dir: Path,
        host_name: Callable[[], str],
        conversation_title: Callable[[str], str | None],
        waiting_count: Callable[[], int],
        sender: Sender | None = None,
    ) -> None:
        self._file = Path(data_dir) / "push_devices.json"
        self._host_name = host_name
        self._conversation_title = conversation_title
        self._waiting_count = waiting_count
        self._sender = sender or _http_sender
        self._tasks: set[asyncio.Task[None]] = set()
        self.relay_url = os.environ.get("POCKET_RELAY_PUSH_URL", DEFAULT_RELAY_URL).strip()
        self.enabled = os.environ.get("POCKET_RELAY_PUSH", "1").strip().lower() not in {"0", "false", "off", "no"}
        self._devices: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------ readiness

    _ready: bool | None = None
    _ready_checked_at: float = 0.0

    async def service_ready(self) -> bool:
        """Whether the push service can deliver alerts (it has the app's Apple push key).

        An empty request is refused with 503 while the service has no key, and with 400 once it
        does. The answer is cached for ten minutes. A phone is only told push is on when this is
        true, so until then it keeps making its own alerts instead of waiting for pushes that never come.
        """
        if not self.enabled or not self.relay_url:
            return False
        now = time.time()
        if self._ready is not None and now - self._ready_checked_at < 600:
            return self._ready
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                response = await client.post(self.relay_url, json={})
            ready = response.status_code != 503 and response.status_code < 500
        except httpx.HTTPError:
            return bool(self._ready)
        self._ready, self._ready_checked_at = ready, now
        return ready

    # ------------------------------------------------------------------ devices

    def _load(self) -> None:
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self._devices = {
                token: info for token, info in raw.items() if _TOKEN_RE.match(token) and isinstance(info, dict)
            }

    def _save(self) -> None:
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._devices, indent=2), encoding="utf-8")
        tmp.replace(self._file)
        try:
            os.chmod(self._file, 0o600)
        except OSError:
            pass

    @staticmethod
    def is_valid_token(token: str) -> bool:
        return bool(_TOKEN_RE.match(token))

    def register(self, token: str, environment: str, show_detail: bool) -> None:
        token = token.lower()
        self._devices[token] = {
            "environment": "sandbox" if environment == "sandbox" else "production",
            "show_detail": bool(show_detail),
            "registered_at": time.time(),
        }
        if len(self._devices) > MAX_DEVICES:
            oldest = sorted(self._devices, key=lambda t: self._devices[t].get("registered_at", 0))
            for stale in oldest[: len(self._devices) - MAX_DEVICES]:
                self._devices.pop(stale, None)
        self._save()

    def unregister(self, token: str) -> bool:
        removed = self._devices.pop(token.lower(), None) is not None
        if removed:
            self._save()
        return removed

    def clear(self) -> None:
        """Forget every phone. Called when the pairing token is rotated, which unpairs them all."""
        if self._devices:
            self._devices = {}
            self._save()

    @property
    def device_count(self) -> int:
        return len(self._devices)

    # ------------------------------------------------------------------ events

    def wrap(self, emit: Emit) -> Emit:
        """Returns an emit function that also sends push alerts for the events that warrant one."""

        async def emit_and_push(event: StreamEvent) -> None:
            await emit(event)
            try:
                self.handle(event)
            except Exception:  # a push problem must never break the agent's turn
                logger.exception("Could not queue a push alert")

        return emit_and_push

    def describe(self, event: StreamEvent) -> dict[str, Any] | None:
        """Turns a stream event into the alert to send, or None when the event needs no alert."""
        payload = event.payload or {}
        if event.type == "approval_request":
            args = payload.get("args") or {}
            command = args.get("CommandLine") or args.get("command")
            detail = _first_line(str(command or payload.get("tool_name") or ""))
            conversation_id = payload.get("conversation_id") or event.conversation_id or ""
            return {
                "kind": "approval",
                "detail": detail,
                "collapse_id": f"approval-{payload.get('id', '')}",
                "data": {"approvalId": str(payload.get("id", "")), "conversationId": str(conversation_id)},
            }
        if event.type == "ask_question":
            questions = payload.get("questions") or []
            first = questions[0].get("question", "") if questions and isinstance(questions[0], dict) else ""
            conversation_id = payload.get("conversation_id") or event.conversation_id or ""
            return {
                "kind": "question",
                "detail": _first_line(str(first)),
                "collapse_id": f"question-{payload.get('id', '')}",
                "data": {"conversationId": str(conversation_id)},
            }
        if event.type == "turn_completed" and event.conversation_id:
            title = self._conversation_title(event.conversation_id) or ""
            return {
                "kind": "finished",
                "detail": _first_line(title),
                "collapse_id": f"finished-{event.conversation_id}",
                "data": {"conversationId": event.conversation_id},
            }
        return None

    def handle(self, event: StreamEvent) -> None:
        if not self.enabled or not self.relay_url or not self._devices:
            return
        alert = self.describe(event)
        if alert is None:
            return
        task = asyncio.get_running_loop().create_task(self._send(alert))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _requests(self, alert: dict[str, Any]) -> list[dict[str, Any]]:
        """One relay request per privacy choice, so detail only goes to phones that asked for it."""
        base = {
            "kind": alert["kind"],
            "host_name": self._host_name()[:60],
            "collapse_id": alert["collapse_id"][:64],
            "data": alert["data"],
            "badge": max(0, int(self._waiting_count())),
        }
        requests: list[dict[str, Any]] = []
        for show_detail in (False, True):
            devices = [
                {"token": token, "environment": info.get("environment", "production")}
                for token, info in self._devices.items()
                if bool(info.get("show_detail")) is show_detail
            ]
            if not devices:
                continue
            body = dict(base, devices=devices)
            if show_detail and alert.get("detail"):
                body["detail"] = alert["detail"]
            requests.append(body)
        return requests

    async def _send(self, alert: dict[str, Any]) -> None:
        for body in self._requests(alert):
            try:
                result = await self._sender(self.relay_url, body)
            except Exception as exc:
                logger.warning("Push alert was not sent: %s", type(exc).__name__)
                continue
            invalid = result.get("invalid_tokens") if isinstance(result, dict) else None
            if isinstance(invalid, list):
                dropped = [t for t in invalid if isinstance(t, str) and self._devices.pop(t.lower(), None) is not None]
                if dropped:
                    self._save()

    async def drain(self) -> None:
        """Waits for alerts that are still being sent (used on shutdown and in tests)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
