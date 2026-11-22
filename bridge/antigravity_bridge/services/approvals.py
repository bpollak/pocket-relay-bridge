"""Interactive approval & AskQuestion broker bridging agent hooks <-> iOS clients."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..models import (
    ApprovalDecision,
    PendingApproval,
    PendingQuestion,
    QuestionItem,
    StreamEvent,
)

EventEmitter = Callable[[StreamEvent], Awaitable[None]]


class ApprovalBroker:
    """Manages pending tool approvals and interactive AskQuestion prompts."""

    def __init__(self, emit: EventEmitter) -> None:
        self._emit = emit
        self._approvals: dict[str, PendingApproval] = {}
        self._approval_futures: dict[str, asyncio.Future[ApprovalDecision]] = {}
        self._questions: dict[str, PendingQuestion] = {}
        self._question_futures: dict[str, asyncio.Future[dict[str, list[str]]]] = {}

    def list_approvals(self) -> list[PendingApproval]:
        return list(self._approvals.values())

    def list_questions(self) -> list[PendingQuestion]:
        return list(self._questions.values())

    async def request_approval(
        self,
        conversation_id: str,
        tool_name: str,
        args: dict[str, Any],
        reason: str = "Action requires user approval under active policy.",
        risk_level: str = "medium",
        timeout_seconds: float = 300.0,
    ) -> ApprovalDecision:
        item = PendingApproval(
            conversation_id=conversation_id,
            tool_name=tool_name,
            args=args,
            reason=reason,
            risk_level=risk_level,
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._approvals[item.id] = item
        self._approval_futures[item.id] = fut

        await self._emit(
            StreamEvent(
                type="approval_request",
                conversation_id=conversation_id,
                payload=item.model_dump(mode="json"),
            )
        )

        answered = False
        reason_closed = "cancelled"
        try:
            decision = await asyncio.wait_for(fut, timeout=timeout_seconds)
            answered = True
            return decision
        except asyncio.TimeoutError:
            reason_closed = "timeout"
            return ApprovalDecision.DENY
        finally:
            self._approvals.pop(item.id, None)
            self._approval_futures.pop(item.id, None)
            if not answered:
                # Timed out or the turn was cancelled: tell clients to drop the card.
                await self._emit_closed("approval_resolved", conversation_id, item.id, reason_closed)

    async def _emit_closed(self, event_type: str, conversation_id: str, item_id: str, reason: str) -> None:
        try:
            await self._emit(
                StreamEvent(
                    type=event_type,
                    conversation_id=conversation_id,
                    payload={"id": item_id, "reason": reason},
                )
            )
        except Exception:
            pass

    async def resolve_approval(self, approval_id: str, decision: ApprovalDecision) -> bool:
        item = self._approvals.get(approval_id)
        fut = self._approval_futures.get(approval_id)
        if not item or not fut or fut.done():
            return False
        fut.set_result(decision)
        await self._emit(
            StreamEvent(
                type="approval_resolved",
                conversation_id=item.conversation_id,
                payload={"id": approval_id, "decision": decision.value},
            )
        )
        return True

    async def request_question(
        self,
        conversation_id: str,
        questions: list[QuestionItem],
        timeout_seconds: float = 600.0,
    ) -> dict[str, list[str]]:
        item = PendingQuestion(conversation_id=conversation_id, questions=questions)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, list[str]]] = loop.create_future()
        self._questions[item.id] = item
        self._question_futures[item.id] = fut

        await self._emit(
            StreamEvent(
                type="ask_question",
                conversation_id=conversation_id,
                payload=item.model_dump(mode="json"),
            )
        )

        answered = False
        reason_closed = "cancelled"
        try:
            answers = await asyncio.wait_for(fut, timeout=timeout_seconds)
            answered = True
            return answers
        except asyncio.TimeoutError:
            reason_closed = "timeout"
            return {}
        finally:
            self._questions.pop(item.id, None)
            self._question_futures.pop(item.id, None)
            if not answered:
                await self._emit_closed("question_resolved", conversation_id, item.id, reason_closed)

    async def resolve_question(self, question_id: str, answers: dict[str, list[str]]) -> bool:
        item = self._questions.get(question_id)
        fut = self._question_futures.get(question_id)
        if not item or not fut or fut.done():
            return False
        fut.set_result(answers)
        await self._emit(
            StreamEvent(
                type="question_resolved",
                conversation_id=item.conversation_id,
                payload={"id": question_id, "answers": answers},
            )
        )
        return True
