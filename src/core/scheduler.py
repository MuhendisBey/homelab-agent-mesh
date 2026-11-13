"""Zero-dependency persistent Asyncio + SQLite Action Scheduler.

Supports deferred Home Assistant commands (e.g., ``10 dk sonra klimayı kapat``)
with:
- Durable persistence in SQLite (survives container restarts).
- Event-driven ``asyncio.Event`` sleep loop (no polling waste).
- Safe-direction misfire policy (overdue shutoff/closing actions always execute;
  overdue turn-on/heating actions past ``misfire_grace_sec`` are skipped safely).
- Execution-time guard enforcement & Actor-Critic ``assert_state`` verification
  with 1 automatic retry for transient non-guard failures.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from src.core.events import (
    ScheduleCreatedEvent,
    ScheduleFiredEvent,
    ScheduleResultEvent,
    emit_to_active_request,
)
from src.core.exceptions import (
    GuardViolationError,
    HomelabAgentError,
    ResourceNotFoundError,
    SafetyBoundViolationError,
)
from src.core.interfaces import EventEmitter, StateAssertionResult

logger = logging.getLogger(__name__)

_CLOSING_SERVICES: frozenset[str] = frozenset(
    {"turn_off", "close_cover", "lock", "media_stop", "media_pause", "stop"}
)

ScheduleStatus = Literal["pending", "running", "done", "failed", "skipped", "cancelled"]
MisfirePolicy = Literal["safe_direction", "execute_always", "skip_if_late"]
ScheduleEventListener = Callable[[BaseModel], Awaitable[None] | None]


class HASchedulerBackend(Protocol):
    """Subset of Home Assistant adapter required by :class:`ActionScheduler`."""

    def resolve_entity_id(self, entity_or_alias: str) -> str:
        """Resolve configured or cached alias into a canonical ``entity_id``."""
        ...

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        service_data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a Home Assistant service call with safety guards."""
        ...

    async def assert_state(
        self,
        entity_id: str,
        expected_state: str | None,
        timeout_sec: float | None = None,
        poll_interval_ms: int | None = None,
        expected_attributes: Mapping[str, Any] | None = None,
    ) -> StateAssertionResult:
        """Verify physical entity state after service execution."""
        ...


class ScheduledActionRecord(BaseModel):
    """Persistent scheduled Home Assistant action record."""

    schedule_id: str
    description: str
    run_at_utc: datetime
    domain: str
    service: str
    entity_id: str
    service_data: dict[str, Any] = Field(default_factory=dict)
    expected_state: str | None = None
    expected_attributes: dict[str, Any] = Field(default_factory=dict)
    misfire_policy: MisfirePolicy = "safe_direction"
    misfire_grace_sec: int = 300
    status: ScheduleStatus = "pending"
    attempts: int = 0
    max_attempts: int = 2
    result: dict[str, Any] | None = None
    session_id: str | None = None
    created_by: str = "agent"
    created_at_utc: datetime
    updated_at_utc: datetime


def is_safe_closing_action(
    domain: str,
    service: str,
    service_data: Mapping[str, Any] | None = None,
) -> bool:
    """True when an action shuts off, closes, locks, or stops equipment safely."""
    clean_domain = domain.strip().lower()
    clean_service = service.strip().lower()
    if clean_service in _CLOSING_SERVICES:
        return True
    if clean_domain == "climate" and clean_service == "set_hvac_mode":
        mode = str((service_data or {}).get("hvac_mode", "")).strip().lower()
        return mode == "off"
    return False


def infer_expected_state(
    domain: str,
    service: str,
    service_data: Mapping[str, Any] | None = None,
) -> str | None:
    """Infer target entity state when omitted by caller."""
    clean_domain = domain.strip().lower()
    clean_service = service.strip().lower()
    if clean_service == "turn_off":
        return "off"
    if clean_service == "turn_on":
        return "on"
    if clean_service == "close_cover":
        return "closed"
    if clean_service == "open_cover":
        return "open"
    if clean_domain == "climate" and clean_service == "set_hvac_mode":
        mode = (service_data or {}).get("hvac_mode")
        return str(mode) if mode is not None else None
    return None


def infer_expected_attributes(
    domain: str,
    service: str,
    service_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Infer target attributes when omitted by caller."""
    clean_domain = domain.strip().lower()
    clean_service = service.strip().lower()
    if clean_domain == "climate" and clean_service == "set_temperature":
        temp = (service_data or {}).get("temperature")
        if temp is not None:
            return {"temperature": temp}
    return {}


class ActionScheduler:
    """Durable SQLite-backed asyncio scheduler for delayed Home Assistant actions."""

    def __init__(
        self,
        db_path: str | Path,
        ha_adapter: HASchedulerBackend,
        emitter: EventEmitter | None = None,
        clock: Callable[[], datetime] | None = None,
        retry_delay_sec: float = 1.0,
    ) -> None:
        self._db_path = Path(db_path)
        self._ha = ha_adapter
        self._emitter = emitter
        self._clock = clock or (lambda: datetime.now(UTC))
        self._retry_delay_sec = retry_delay_sec

        self._write_lock = asyncio.Lock()
        self._exec_lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._listeners: list[ScheduleEventListener] = []
        self._task: asyncio.Task[None] | None = None
        self._running = False

        self._init_db_sync()

    def _now(self) -> datetime:
        now = self._clock()
        return now if now.tzinfo is not None else now.replace(tzinfo=UTC)

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db_sync(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_actions (
                    schedule_id TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    run_at_utc TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    service TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    service_data_json TEXT NOT NULL,
                    expected_state TEXT,
                    expected_attributes_json TEXT NOT NULL,
                    misfire_policy TEXT NOT NULL DEFAULT 'safe_direction',
                    misfire_grace_sec INTEGER NOT NULL DEFAULT 300,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 2,
                    result_json TEXT,
                    session_id TEXT,
                    created_by TEXT NOT NULL DEFAULT 'agent',
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scheduled_actions_status_run
                ON scheduled_actions (status, run_at_utc)
                """
            )

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> ScheduledActionRecord:
        run_at = datetime.fromisoformat(str(row[2]))
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=UTC)
        created_at = datetime.fromisoformat(str(row[17]))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        updated_at = datetime.fromisoformat(str(row[18]))
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return ScheduledActionRecord(
            schedule_id=str(row[0]),
            description=str(row[1]),
            run_at_utc=run_at,
            domain=str(row[3]),
            service=str(row[4]),
            entity_id=str(row[5]),
            service_data=json.loads(str(row[6] or "{}")),
            expected_state=str(row[7]) if row[7] is not None else None,
            expected_attributes=json.loads(str(row[8] or "{}")),
            misfire_policy=str(row[9]),  # type: ignore[arg-type]
            misfire_grace_sec=int(row[10]),
            status=str(row[11]),  # type: ignore[arg-type]
            attempts=int(row[12]),
            max_attempts=int(row[13]),
            result=json.loads(str(row[14])) if row[14] else None,
            session_id=str(row[15]) if row[15] is not None else None,
            created_by=str(row[16]),
            created_at_utc=created_at,
            updated_at_utc=updated_at,
        )

    def _recover_interrupted_sync(self) -> int:
        now_iso = self._now().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE scheduled_actions
                SET status = 'pending', updated_at_utc = ?
                WHERE status = 'running'
                """,
                (now_iso,),
            )
            return int(cur.rowcount)

    def _insert_sync(self, rec: ScheduledActionRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_actions (
                    schedule_id, description, run_at_utc, domain, service, entity_id,
                    service_data_json, expected_state, expected_attributes_json,
                    misfire_policy, misfire_grace_sec, status, attempts, max_attempts,
                    result_json, session_id, created_by, created_at_utc, updated_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.schedule_id,
                    rec.description,
                    rec.run_at_utc.astimezone(UTC).isoformat(),
                    rec.domain,
                    rec.service,
                    rec.entity_id,
                    json.dumps(rec.service_data),
                    rec.expected_state,
                    json.dumps(rec.expected_attributes),
                    rec.misfire_policy,
                    rec.misfire_grace_sec,
                    rec.status,
                    rec.attempts,
                    rec.max_attempts,
                    json.dumps(rec.result) if rec.result is not None else None,
                    rec.session_id,
                    rec.created_by,
                    rec.created_at_utc.astimezone(UTC).isoformat(),
                    rec.updated_at_utc.astimezone(UTC).isoformat(),
                ),
            )

    def _fetch_one_sync(self, schedule_id: str) -> ScheduledActionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scheduled_actions WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def _list_sync(
        self,
        status: ScheduleStatus | None = None,
        entity_id: str | None = None,
    ) -> list[ScheduledActionRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM scheduled_actions {where} ORDER BY run_at_utc ASC",
                tuple(params),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def _fetch_next_pending_sync(self) -> ScheduledActionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM scheduled_actions
                WHERE status = 'pending'
                ORDER BY run_at_utc ASC
                LIMIT 1
                """
            ).fetchone()
        return self._row_to_record(row) if row else None

    def _update_state_sync(
        self,
        schedule_id: str,
        status: ScheduleStatus,
        attempts: int,
        result: dict[str, Any] | None,
    ) -> ScheduledActionRecord | None:
        now_iso = self._now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_actions
                SET status = ?, attempts = ?, result_json = ?, updated_at_utc = ?
                WHERE schedule_id = ?
                """,
                (
                    status,
                    attempts,
                    json.dumps(result) if result is not None else None,
                    now_iso,
                    schedule_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM scheduled_actions WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    # ------------------------------------------------------------------
    # Event Listeners & Lifecycle
    # ------------------------------------------------------------------

    def add_event_listener(self, listener: ScheduleEventListener) -> None:
        """Register a sync or async callback invoked on schedule lifecycle events."""
        self._listeners.append(listener)

    async def _emit(self, event: BaseModel) -> None:
        if self._emitter is not None:
            self._emitter.emit(event)
        emit_to_active_request(event)
        for listener in list(self._listeners):
            try:
                maybe_awaitable = listener(event)
                if asyncio.iscoroutine(maybe_awaitable) or isinstance(maybe_awaitable, Awaitable):
                    await maybe_awaitable
            except Exception:
                logger.exception("scheduler | event listener raised an error")

    async def start(self) -> None:
        """Recover interrupted jobs and start the background event-driven scheduler loop."""
        self._init_db_sync()
        async with self._write_lock:
            recovered = await asyncio.to_thread(self._recover_interrupted_sync)
        if recovered:
            logger.info("scheduler | recovered %d interrupted job(s) to pending", recovered)
        self._running = True
        self._wakeup.clear()
        self._task = asyncio.create_task(self._run_loop(), name="action_scheduler_loop")
        logger.info("scheduler | started")

    async def stop(self) -> None:
        """Stop the scheduler background loop cleanly."""
        self._running = False
        self._wakeup.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("scheduler | stopped")

    # ------------------------------------------------------------------
    # Public Scheduling API
    # ------------------------------------------------------------------

    async def schedule_action(
        self,
        domain: str,
        service: str,
        entity_id: str,
        delay_seconds: int | None = None,
        run_at_utc: datetime | None = None,
        service_data: Mapping[str, Any] | None = None,
        expected_state: str | None = None,
        expected_attributes: Mapping[str, Any] | None = None,
        description: str = "",
        misfire_policy: MisfirePolicy = "safe_direction",
        misfire_grace_sec: int = 300,
        max_attempts: int = 2,
        session_id: str | None = None,
        created_by: str = "agent",
    ) -> ScheduledActionRecord:
        """Persist a new deferred action and wake the scheduler loop."""
        now = self._now()
        if run_at_utc is not None:
            target_time = run_at_utc if run_at_utc.tzinfo is not None else run_at_utc.replace(tzinfo=UTC)
            target_time = target_time.astimezone(UTC)
        elif delay_seconds is not None:
            if not 1 <= delay_seconds <= 86400:
                raise SafetyBoundViolationError(
                    f"delay_seconds={delay_seconds} must be between 1 and 86400 (24h).",
                    delay_seconds=delay_seconds,
                )
            target_time = now + timedelta(seconds=delay_seconds)
        else:
            raise SafetyBoundViolationError("Either delay_seconds or run_at_utc must be provided.")

        canonical = self._ha.resolve_entity_id(entity_id)
        clean_domain = domain.strip().lower()
        clean_service = service.strip().lower()
        data_dict = dict(service_data or {})

        eff_expected_state = (
            expected_state
            if expected_state is not None
            else infer_expected_state(clean_domain, clean_service, data_dict)
        )
        eff_expected_attrs = (
            dict(expected_attributes)
            if expected_attributes is not None
            else infer_expected_attributes(clean_domain, clean_service, data_dict)
        )
        eff_desc = description.strip() or f"{clean_domain}.{clean_service} -> {canonical}"

        record = ScheduledActionRecord(
            schedule_id=uuid.uuid4().hex[:12],
            description=eff_desc,
            run_at_utc=target_time,
            domain=clean_domain,
            service=clean_service,
            entity_id=canonical,
            service_data=data_dict,
            expected_state=eff_expected_state,
            expected_attributes=eff_expected_attrs,
            misfire_policy=misfire_policy,
            misfire_grace_sec=max(0, misfire_grace_sec),
            status="pending",
            attempts=0,
            max_attempts=max(1, max_attempts),
            result=None,
            session_id=session_id,
            created_by=created_by,
            created_at_utc=now,
            updated_at_utc=now,
        )

        async with self._write_lock:
            await asyncio.to_thread(self._insert_sync, record)

        self._wakeup.set()
        await self._emit(
            ScheduleCreatedEvent(
                schedule_id=record.schedule_id,
                description=record.description,
                entity_id=record.entity_id,
                domain=record.domain,
                service=record.service,
                run_at_utc=record.run_at_utc,
            )
        )
        logger.info(
            "scheduler | created id=%s (%s.%s -> %s at %s)",
            record.schedule_id,
            record.domain,
            record.service,
            record.entity_id,
            record.run_at_utc.isoformat(),
        )
        return record

    async def get_action(self, schedule_id: str) -> ScheduledActionRecord:
        rec = await asyncio.to_thread(self._fetch_one_sync, schedule_id)
        if rec is None:
            raise ResourceNotFoundError(
                f"Scheduled action '{schedule_id}' was not found.",
                schedule_id=schedule_id,
            )
        return rec

    async def list_actions(
        self,
        status: ScheduleStatus | None = None,
        entity_id: str | None = None,
    ) -> list[ScheduledActionRecord]:
        canonical = self._ha.resolve_entity_id(entity_id) if entity_id else None
        return await asyncio.to_thread(self._list_sync, status, canonical)

    async def cancel_action(self, schedule_id: str) -> ScheduledActionRecord:
        async with self._write_lock:
            existing = await asyncio.to_thread(self._fetch_one_sync, schedule_id)
            if existing is None:
                raise ResourceNotFoundError(
                    f"Scheduled action '{schedule_id}' was not found.",
                    schedule_id=schedule_id,
                )
            if existing.status != "pending":
                raise GuardViolationError(
                    f"Cannot cancel scheduled action '{schedule_id}' in status '{existing.status}'.",
                    schedule_id=schedule_id,
                    status=existing.status,
                )
            updated = await asyncio.to_thread(
                self._update_state_sync,
                schedule_id,
                "cancelled",
                existing.attempts,
                {"reason": "cancelled_by_request"},
            )
        self._wakeup.set()
        assert updated is not None
        await self._emit(
            ScheduleResultEvent(
                schedule_id=updated.schedule_id,
                entity_id=updated.entity_id,
                status="cancelled",
                verified=False,
                detail="Cancelled before execution.",
            )
        )
        return updated

    # ------------------------------------------------------------------
    # Execution Engine
    # ------------------------------------------------------------------

    async def run_due_once(self) -> int:
        """Process all currently due pending actions once (also useful in deterministic tests)."""
        async with self._exec_lock:
            processed = 0
            while True:
                next_rec = await asyncio.to_thread(self._fetch_next_pending_sync)
                if next_rec is None:
                    break
                now = self._now()
                if next_rec.run_at_utc > now:
                    break
                await self._execute_record(next_rec)
                processed += 1
            return processed

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.run_due_once()
                next_rec = await asyncio.to_thread(self._fetch_next_pending_sync)
                if next_rec is None:
                    timeout = 60.0
                else:
                    delta = (next_rec.run_at_utc - self._now()).total_seconds()
                    if delta <= 0:
                        continue
                    timeout = min(delta, 60.0)

                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
                except TimeoutError:
                    pass
                self._wakeup.clear()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("scheduler | loop error; retrying in 2s")
                await asyncio.sleep(2.0)

    async def _execute_record(self, rec: ScheduledActionRecord) -> ScheduledActionRecord:
        now = self._now()
        late_sec = (now - rec.run_at_utc).total_seconds()

        # Check safe-direction misfire policy before execution
        if late_sec > rec.misfire_grace_sec:
            should_run = rec.misfire_policy == "execute_always" or (
                rec.misfire_policy == "safe_direction"
                and is_safe_closing_action(rec.domain, rec.service, rec.service_data)
            )
            if not should_run:
                result_payload = {
                    "reason": "misfire_grace_exceeded",
                    "late_sec": round(late_sec, 1),
                    "policy": rec.misfire_policy,
                }
                async with self._write_lock:
                    skipped = await asyncio.to_thread(
                        self._update_state_sync,
                        rec.schedule_id,
                        "skipped",
                        rec.attempts,
                        result_payload,
                    )
                assert skipped is not None
                await self._emit(
                    ScheduleResultEvent(
                        schedule_id=skipped.schedule_id,
                        entity_id=skipped.entity_id,
                        status="skipped",
                        verified=False,
                        detail=(
                            f"Skipped overdue non-closing action (late by {late_sec:.0f}s, "
                            f"policy={rec.misfire_policy})."
                        ),
                    )
                )
                return skipped

        async with self._write_lock:
            await asyncio.to_thread(self._update_state_sync, rec.schedule_id, "running", rec.attempts, None)

        last_result: dict[str, Any] = {}
        final_status: Literal["done", "failed"] = "failed"
        verified = False
        detail_msg: str | None = None
        attempts_done = rec.attempts

        for attempt_idx in range(1, max(1, rec.max_attempts) + 1):
            attempts_done = attempt_idx
            await self._emit(
                ScheduleFiredEvent(
                    schedule_id=rec.schedule_id,
                    entity_id=rec.entity_id,
                    domain=rec.domain,
                    service=rec.service,
                    attempt=attempt_idx,
                )
            )
            try:
                await self._ha.call_service(
                    domain=rec.domain,
                    service=rec.service,
                    entity_id=rec.entity_id,
                    service_data=rec.service_data,
                )
                assertion = await self._ha.assert_state(
                    entity_id=rec.entity_id,
                    expected_state=rec.expected_state,
                    expected_attributes=rec.expected_attributes,
                )
                last_result = assertion.model_dump(mode="json")
                if assertion.verified:
                    final_status = "done"
                    verified = True
                    detail_msg = "Verified."
                    break
                detail_msg = assertion.reason or "State assertion failed."
            except GuardViolationError as exc:
                last_result = exc.to_dict()
                detail_msg = exc.message
                final_status = "failed"
                break
            except HomelabAgentError as exc:
                last_result = exc.to_dict()
                detail_msg = exc.message
            except Exception as exc:
                last_result = {"error": "execution_error", "message": str(exc)}
                detail_msg = str(exc)

            if attempt_idx < rec.max_attempts and self._retry_delay_sec > 0:
                await asyncio.sleep(self._retry_delay_sec)

        async with self._write_lock:
            finished = await asyncio.to_thread(
                self._update_state_sync,
                rec.schedule_id,
                final_status,
                attempts_done,
                last_result,
            )
        assert finished is not None
        await self._emit(
            ScheduleResultEvent(
                schedule_id=finished.schedule_id,
                entity_id=finished.entity_id,
                status=final_status,
                verified=verified,
                detail=detail_msg,
            )
        )
        logger.info(
            "scheduler | id=%s finished status=%s verified=%s attempts=%d",
            finished.schedule_id,
            finished.status,
            verified,
            finished.attempts,
        )
        return finished
