"""Recurring cron and one-time timer scheduler for Pocket Relay."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from croniter import croniter

from ..config import BridgeStore
from ..models import (
    CreateScheduledTaskRequest,
    ScheduledTask,
    ScheduledTaskRun,
    StreamEvent,
    TaskScheduleKind,
    UpdateScheduledTaskRequest,
    utc_now,
)
from .conversations import ConversationService

EventEmitter = Callable[[StreamEvent], Awaitable[None]]

MAX_TIMER_DELAY_SECONDS = 10 * 366 * 24 * 3600  # ~10 years


class SchedulerService:
    def __init__(
        self,
        store: BridgeStore,
        conversations: ConversationService,
        emit: EventEmitter,
    ) -> None:
        self.store = store
        self.conversations = conversations
        self._emit = emit
        self._loop_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            for task in self.store.scheduled_tasks.values():
                if task.enabled and task.next_run_at is None:
                    task.next_run_at = self._compute_next_run(task)
            self._loop_task = asyncio.create_task(self._tick_loop(), name="antigravity-scheduler")

    async def stop(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

    def list_tasks(self) -> list[ScheduledTask]:
        items = list(self.store.scheduled_tasks.values())
        items.sort(key=lambda t: t.created_at, reverse=True)
        return items

    def create_task(self, req: CreateScheduledTaskRequest) -> ScheduledTask:
        cron_expr: str | None = None
        if req.kind == TaskScheduleKind.CRON:
            cron_expr = req.cron_expression or "0 9 * * *"
            if not croniter.is_valid(cron_expr):
                raise ValueError(f"Invalid cron expression: {cron_expr}")
        if req.kind == TaskScheduleKind.TIMER and req.delay_seconds is not None:
            if req.delay_seconds < 0 or req.delay_seconds > MAX_TIMER_DELAY_SECONDS:
                raise ValueError(
                    f"delay_seconds must be between 0 and {MAX_TIMER_DELAY_SECONDS}."
                )
        task = ScheduledTask(
            name=req.name,
            prompt=req.prompt,
            kind=req.kind,
            cron_expression=cron_expr,
            delay_seconds=req.delay_seconds if req.kind == TaskScheduleKind.TIMER else None,
            workspace_id=req.workspace_id,
            model=req.model,
            enabled=req.enabled,
        )
        task.next_run_at = self._compute_next_run(task) if task.enabled else None
        self.store.scheduled_tasks[task.id] = task
        self.store.save()
        return task

    def update_task(self, task_id: str, req: UpdateScheduledTaskRequest) -> ScheduledTask:
        task = self.store.scheduled_tasks.get(task_id)
        if not task:
            raise KeyError("Scheduled task not found")
        if req.name is not None:
            task.name = req.name
        if req.prompt is not None:
            task.prompt = req.prompt
        if req.cron_expression is not None:
            if not croniter.is_valid(req.cron_expression):
                raise ValueError(f"Invalid cron expression: {req.cron_expression}")
            task.cron_expression = req.cron_expression
        if req.workspace_id is not None:
            task.workspace_id = req.workspace_id
        if req.model is not None:
            task.model = req.model
        if req.enabled is not None:
            task.enabled = req.enabled
        task.next_run_at = self._compute_next_run(task) if task.enabled else None
        self.store.save()
        return task

    def delete_task(self, task_id: str) -> bool:
        removed = self.store.scheduled_tasks.pop(task_id, None)
        if removed:
            self.store.save()
            return True
        return False

    def _compute_next_run(self, task: ScheduledTask, base: datetime | None = None) -> datetime | None:
        now = base or utc_now()
        if task.kind == TaskScheduleKind.TIMER:
            secs = max(5, task.delay_seconds or 60)
            try:
                return now + timedelta(seconds=secs)
            except OverflowError:
                return None
        if task.cron_expression and croniter.is_valid(task.cron_expression):
            # Cron fields mean wall-clock time on this machine. Iterate on naive local
            # time and convert back with the offset in force at that moment (DST-safe).
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            local_naive = now.astimezone().replace(tzinfo=None)
            nxt_local = croniter(task.cron_expression, local_naive).get_next(datetime)
            return nxt_local.astimezone().astimezone(timezone.utc)
        return None

    async def run_now(self, task_id: str) -> ScheduledTaskRun:
        task = self.store.scheduled_tasks.get(task_id)
        if not task:
            raise KeyError("Scheduled task not found")
        run = ScheduledTaskRun(status="running")
        conv = self.conversations.create_conversation(
            title=f"[Scheduled] {task.name}",
            workspace_id=task.workspace_id,
            model=task.model,
        )
        run.conversation_id = conv.id
        task.runs.insert(0, run)
        task.runs = task.runs[:20]
        task.last_run_at = utc_now()
        self.store.save()

        try:
            await self.conversations.send_message(
                conversation_id=conv.id,
                content=task.prompt,
                model=task.model,
            )
            run.status = "succeeded"
            run.summary = f"Dispatched to conversation {conv.id}"
        except Exception as exc:
            run.status = "failed"
            run.summary = str(exc)
        finally:
            run.completed_at = utc_now()
            self.store.save()
            await self._emit(
                StreamEvent(
                    type="scheduled_task_run",
                    conversation_id=conv.id,
                    payload={"task_id": task.id, "run": run.model_dump(mode="json")},
                )
            )
        return run

    async def _tick_loop(self) -> None:
        while True:
            try:
                now = utc_now()
                for task in list(self.store.scheduled_tasks.values()):
                    if not task.enabled or task.next_run_at is None:
                        continue
                    if task.next_run_at <= now:
                        if task.kind == TaskScheduleKind.TIMER:
                            task.enabled = False
                            task.next_run_at = None
                        else:
                            task.next_run_at = self._compute_next_run(task, base=now)
                        self.store.save()
                        asyncio.create_task(self.run_now(task.id))
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(5.0)
