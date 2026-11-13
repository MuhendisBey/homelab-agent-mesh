"""Structured lifecycle events streamed to clients via SSE.

Defined in ``core`` (not ``api``) so agents/hooks can emit typed events without
depending on the HTTP layer. ``src/api/schemas.py`` re-exports them.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class _BaseEvent(BaseModel):
    ts: datetime = Field(default_factory=_now)


class SessionStartedEvent(_BaseEvent):
    type: Literal["session"] = "session"
    session_id: str


class PlanStepEvent(_BaseEvent):
    type: Literal["plan_step"] = "plan_step"
    agent: str
    step: int
    detail: str


class ToolCallStartEvent(_BaseEvent):
    type: Literal["tool_call_start"] = "tool_call_start"
    agent: str
    tool: str
    tool_use_id: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolCallEndEvent(_BaseEvent):
    type: Literal["tool_call_end"] = "tool_call_end"
    agent: str
    tool: str
    tool_use_id: str
    status: Literal["success", "error", "cancelled"]
    duration_ms: int | None = None
    detail: str | None = None


class StateAssertionEvent(_BaseEvent):
    type: Literal["state_assertion"] = "state_assertion"
    agent: str
    entity: str
    verified: bool
    expected_state: str | None = None
    actual_state: str | None = None
    reason: str | None = None


class CriticVerdictEvent(_BaseEvent):
    type: Literal["critic_verdict"] = "critic_verdict"
    agent: str
    mutations: int
    verified: int
    failed: int
    unverified_mutations: list[str] = Field(default_factory=list)


class MemoryInjectedEvent(_BaseEvent):
    type: Literal["memory_injected"] = "memory_injected"
    agent: str
    domain: str
    core_rules: int
    semantic_rules: int
    degraded: bool = False


class InterventionRequiredEvent(_BaseEvent):
    type: Literal["intervention_required"] = "intervention_required"
    approval_id: str
    agent: str | None = None
    tool: str | None = None
    operation: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str
    expires_at: datetime


class InterventionResolvedEvent(_BaseEvent):
    type: Literal["intervention_resolved"] = "intervention_resolved"
    approval_id: str
    approved: bool
    expired: bool = False


class ScheduleCreatedEvent(_BaseEvent):
    type: Literal["schedule_created"] = "schedule_created"
    schedule_id: str
    description: str
    entity_id: str
    domain: str
    service: str
    run_at_utc: datetime


class ScheduleFiredEvent(_BaseEvent):
    type: Literal["schedule_fired"] = "schedule_fired"
    schedule_id: str
    entity_id: str
    domain: str
    service: str
    attempt: int


class ScheduleResultEvent(_BaseEvent):
    type: Literal["schedule_result"] = "schedule_result"
    schedule_id: str
    entity_id: str
    status: Literal["done", "failed", "skipped", "cancelled"]
    verified: bool = False
    detail: str | None = None


class ContentDeltaEvent(_BaseEvent):
    type: Literal["content_delta"] = "content_delta"
    agent: str
    text: str


class ErrorEvent(_BaseEvent):
    type: Literal["error"] = "error"
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DoneEvent(_BaseEvent):
    type: Literal["done"] = "done"
    session_id: str
    stop_reason: str
    pending_approvals: list[str] = Field(default_factory=list)


StreamEvent = Annotated[
    SessionStartedEvent
    | PlanStepEvent
    | ToolCallStartEvent
    | ToolCallEndEvent
    | StateAssertionEvent
    | CriticVerdictEvent
    | MemoryInjectedEvent
    | InterventionRequiredEvent
    | InterventionResolvedEvent
    | ScheduleCreatedEvent
    | ScheduleFiredEvent
    | ScheduleResultEvent
    | ContentDeltaEvent
    | ErrorEvent
    | DoneEvent,
    Field(discriminator="type"),
]


class QueueEventSink:
    """Thread-safe bridge from agent hooks to the SSE generator (one per request)."""

    def __init__(self) -> None:
        # Must be constructed inside the event loop that consumes the stream.
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._queue: asyncio.Queue[BaseModel | None] = asyncio.Queue()
        self._closed = False

    def _put(self, item: BaseModel | None) -> None:
        try:
            if threading.get_ident() == self._loop_thread:
                self._queue.put_nowait(item)
            else:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:  # loop closed
            logger.debug("event sink loop closed; dropping item")

    def emit(self, event: BaseModel) -> None:
        """Enqueue an event from any thread; silently dropped once closed."""
        if not self._closed:
            self._put(event)

    def close(self) -> None:
        """Signal end-of-stream to the consumer (idempotent)."""
        if not self._closed:
            self._closed = True
            self._put(None)

    async def get(self, timeout: float) -> BaseModel | None:
        """Next event; raises :class:`TimeoutError` when idle for ``timeout`` seconds."""
        return await asyncio.wait_for(self._queue.get(), timeout=timeout)


class SwitchableEmitter:
    """Session-scoped emitter whose target sink is swapped per request.

    Long-lived agents/hooks hold this object; the API layer attaches a fresh
    :class:`QueueEventSink` for each streaming request and detaches it afterwards.
    """

    def __init__(self) -> None:
        self._sink: QueueEventSink | None = None

    def attach(self, sink: QueueEventSink) -> None:
        self._sink = sink

    def detach(self, sink: QueueEventSink) -> None:
        if self._sink is sink:
            self._sink = None

    def emit(self, event: BaseModel) -> None:
        sink = self._sink
        if sink is not None:
            sink.emit(event)


_ACTIVE_REQUEST_EMITTER: contextvars.ContextVar[SwitchableEmitter | QueueEventSink | None] = (
    contextvars.ContextVar("active_request_emitter", default=None)
)


def set_active_request_emitter(
    emitter: SwitchableEmitter | QueueEventSink | None,
) -> contextvars.Token[SwitchableEmitter | QueueEventSink | None]:
    """Bind the active request's event emitter to the current async context."""
    return _ACTIVE_REQUEST_EMITTER.set(emitter)


def reset_active_request_emitter(
    token: contextvars.Token[SwitchableEmitter | QueueEventSink | None],
) -> None:
    """Restore the previous request event emitter context."""
    _ACTIVE_REQUEST_EMITTER.reset(token)


def emit_to_active_request(event: BaseModel) -> None:
    """Emit an event to the currently active SSE request stream, if any."""
    emitter = _ACTIVE_REQUEST_EMITTER.get()
    if emitter is not None:
        emitter.emit(event)
