"""Push alerts: device registration and what is sent to the relay."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from antigravity_bridge.models import StreamEvent
from antigravity_bridge.services.push import PushService

from test_bridge import auth_headers, client_and_token  # noqa: F401  (fixture)

TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


def make_service(tmp_path: Path, sent: list, reply: dict | None = None, waiting: int = 1) -> PushService:
    async def sender(url: str, body: dict) -> dict:
        sent.append((url, body))
        return reply or {}

    return PushService(
        data_dir=tmp_path,
        host_name=lambda: "Studio",
        conversation_title=lambda cid: "Add search" if cid == "c1" else None,
        waiting_count=lambda: waiting,
        sender=sender,
    )


def approval_event() -> StreamEvent:
    return StreamEvent(
        type="approval_request",
        conversation_id="c1",
        payload={"id": "ap1", "conversation_id": "c1", "tool_name": "run_command",
                 "args": {"CommandLine": "swift test\nrm -rf build"}},
    )


async def test_detail_only_goes_to_phones_that_asked_for_it(tmp_path: Path):
    sent: list = []
    push = make_service(tmp_path, sent)
    push.register(TOKEN_A, "production", show_detail=False)
    push.register(TOKEN_B, "sandbox", show_detail=True)

    push.handle(approval_event())
    await push.drain()

    assert len(sent) == 2
    private = next(body for _, body in sent if body["devices"][0]["token"] == TOKEN_A)
    detailed = next(body for _, body in sent if body["devices"][0]["token"] == TOKEN_B)
    assert "detail" not in private
    assert "swift test" not in str(private)
    assert detailed["detail"] == "swift test"
    assert detailed["devices"][0]["environment"] == "sandbox"
    for body in (private, detailed):
        assert body["kind"] == "approval"
        assert body["host_name"] == "Studio"
        assert body["collapse_id"] == "approval-ap1"
        assert body["data"] == {"approvalId": "ap1", "conversationId": "c1"}
        assert body["badge"] == 1


async def test_question_and_finished_alerts_and_ignored_events(tmp_path: Path):
    sent: list = []
    push = make_service(tmp_path, sent, waiting=0)
    push.register(TOKEN_B, "production", show_detail=True)

    push.handle(StreamEvent(type="ask_question", conversation_id="c1",
                            payload={"id": "q1", "conversation_id": "c1",
                                     "questions": [{"question": "Which database?", "options": []}]}))
    push.handle(StreamEvent(type="turn_completed", conversation_id="c1", payload={"message": {"content": "done"}}))
    push.handle(StreamEvent(type="token", conversation_id="c1", payload={"delta": "secret text"}))
    push.handle(StreamEvent(type="terminal_output", payload={"chunk": "secret"}))
    await push.drain()

    kinds = [body["kind"] for _, body in sent]
    assert kinds == ["question", "finished"]
    assert sent[0][1]["detail"] == "Which database?"
    assert sent[1][1]["detail"] == "Add search"
    assert "done" not in str(sent[1][1]), "The reply text itself is never sent to the relay"
    assert "secret" not in str(sent)


async def test_nothing_is_sent_without_devices_or_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sent: list = []
    push = make_service(tmp_path, sent)
    push.handle(approval_event())
    await push.drain()
    assert sent == []

    monkeypatch.setenv("POCKET_RELAY_PUSH", "0")
    off = make_service(tmp_path, sent)
    off.register(TOKEN_A, "production", show_detail=False)
    off.handle(approval_event())
    await off.drain()
    assert sent == []


async def test_devices_persist_are_capped_and_dead_tokens_are_dropped(tmp_path: Path):
    sent: list = []
    push = make_service(tmp_path, sent, reply={"invalid_tokens": [TOKEN_A]})
    push.register(TOKEN_A, "production", show_detail=False)
    push.register(TOKEN_B, "production", show_detail=False)
    assert make_service(tmp_path, []).device_count == 2, "Registrations survive a restart"

    push.handle(approval_event())
    await push.drain()
    assert push.device_count == 1
    assert make_service(tmp_path, []).device_count == 1

    for i in range(15):
        push.register(f"{i:02x}" * 32, "production", show_detail=False)
    assert push.device_count == 10


async def test_relay_failure_does_not_raise_or_break_the_event(tmp_path: Path):
    async def failing(url: str, body: dict) -> dict:
        raise RuntimeError("relay down")

    push = PushService(tmp_path, lambda: "Studio", lambda cid: None, lambda: 0, sender=failing)
    push.register(TOKEN_A, "production", show_detail=False)
    delivered: list = []

    async def emit(event: StreamEvent) -> None:
        delivered.append(event.type)

    await push.wrap(emit)(approval_event())
    await push.drain()
    assert delivered == ["approval_request"]


def test_push_device_endpoints(client_and_token):  # noqa: F811
    client, token, _ = client_and_token
    push = client.app.state.push

    assert client.post("/api/push/devices", json={"device_token": TOKEN_A}).status_code == 401
    bad = client.post("/api/push/devices", json={"device_token": "not-a-token"}, headers=auth_headers(token))
    assert bad.status_code == 422

    # Until the push service has the app's Apple key, phones are told no, so they keep on-device alerts.
    push._ready, push._ready_checked_at = False, time.time()
    not_yet = client.post("/api/push/devices", json={"device_token": TOKEN_A}, headers=auth_headers(token))
    assert not_yet.status_code == 503 and push.device_count == 0
    push._ready, push._ready_checked_at = True, time.time()

    ok = client.post("/api/push/devices",
                     json={"device_token": TOKEN_A.upper(), "environment": "sandbox", "show_detail": True},
                     headers=auth_headers(token))
    assert ok.status_code == 200 and ok.json()["registered"] is True
    assert push.device_count == 1

    removed = client.delete(f"/api/push/devices/{TOKEN_A}", headers=auth_headers(token))
    assert removed.json() == {"removed": True}
    assert push.device_count == 0

    client.post("/api/push/devices", json={"device_token": TOKEN_B}, headers=auth_headers(token))
    client.post("/api/pairing/rotate", headers=auth_headers(token))
    assert push.device_count == 0, "Rotating the pairing token unpairs every phone, so their alerts stop too"
