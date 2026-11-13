"""Root Supervisor Orchestrator with Sliding-Window Memory, Two-Phase Commit & SSE Streaming.

Responsibilities:
- Multi-intent task decomposition across Home Assistant (`home_assistant_agent`) and
  Infrastructure (`infrastructure_agent`) sub-agents via Strands `.as_tool()`.
- Short-term conversation memory via `SlidingWindowConversationManager`.
- Self-healing rule persistence via `record_lesson_learned`.
- Two-Phase Commit (Human-in-the-Loop) interrupt suspension, token issuance, and
  resume via `/api/v1/interventions/approve`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterToolCallEvent, BeforeToolCallEvent
from strands.interrupt import Interrupt

from src.agents.factory import AgentFactory, SafetyAndApprovalGate
from src.core.config import AppConfig
from src.core.events import (
    ContentDeltaEvent,
    DoneEvent,
    ErrorEvent,
    InterventionRequiredEvent,
    InterventionResolvedEvent,
    PlanStepEvent,
    QueueEventSink,
    SessionStartedEvent,
    StreamEvent,
    SwitchableEmitter,
    ToolCallEndEvent,
    ToolCallStartEvent,
    reset_active_request_emitter,
    set_active_request_emitter,
)
from src.core.exceptions import (
    HomelabAgentError,
    InterventionNotFoundError,
    PendingInterventionError,
    SessionBusyError,
)
from src.core.interfaces import EventEmitter
from src.memory.base import BaseMemoryStore
from src.memory.reflection_store import create_record_lesson_tool

logger = logging.getLogger(__name__)

_SUPERVISOR_BASE_PROMPT = """
You are the Root Supervisor Agent of the Homelab & Smart Home Orchestrator.

### Core Responsibilities
1. **Task Decomposition & Routing**:
   - Break multi-intent user prompts into an ordered plan and delegate domain tasks to your
     specialist sub-agents (`home_assistant_agent`, `infrastructure_agent`).
2. **Actor-Critic Verification**:
   - Require sub-agents to report both the action taken and the post-action verification
     (`assert_entity_state` for Home Assistant; container/guest status verification for
     Infrastructure).
   - Never report an action as successful if its state assertion failed or was blocked by a
     safety guard.
3. **Self-Healing Reflection Memory**:
   - When a recurring device quirk, entity mapping, or logical parameter fix is confirmed,
     call `record_lesson_learned` with the appropriate domain (`ha` or `infra`) so future
     tasks automatically apply it.
   - NEVER record transient network, timeout, or circuit-breaker errors (e.g., `TimeoutError`,
     `ConnectionError`, `circuit_open`, or temporary reachability failures) into memory.
4. **Human-in-the-Loop Interventions**:
   - Destructive operations automatically pause for human confirmation. If a user request
     contains multiple destructive operations, preserve and execute every remaining step in
     order as each `approval_id` is resolved.
   - If an operation is denied by the user, acknowledge the denial and do not retry the denied action.
""".strip()


class SupervisorLifecycleHooks(HookProvider):
    """Emits `plan_step` and supervisor-level `tool_call_start`/`tool_call_end` SSE events."""

    def __init__(self, agent_name: str, emitter: EventEmitter) -> None:
        self._agent_name = agent_name
        self._emitter = emitter
        self._step_counter = 0

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.on_after_tool_call)

    def on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        self._step_counter += 1
        tool_name = str(event.tool_use.get("name", ""))
        tool_use_id = str(event.tool_use.get("toolUseId", ""))
        raw_input = event.tool_use.get("input")
        args = dict(raw_input) if isinstance(raw_input, Mapping) else {"input": raw_input}
        detail = str(args.get("input") or json.dumps(args, ensure_ascii=False))
        self._emitter.emit(
            PlanStepEvent(
                agent=self._agent_name,
                step=self._step_counter,
                detail=f"Delegating to {tool_name}: {detail[:240]}",
            )
        )
        self._emitter.emit(
            ToolCallStartEvent(
                agent=self._agent_name,
                tool=tool_name,
                tool_use_id=tool_use_id,
                args=args,
            )
        )

    def on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        tool_name = str(event.tool_use.get("name", ""))
        tool_use_id = str(event.tool_use.get("toolUseId", ""))
        status_raw = str(event.result.get("status", "success"))
        if event.cancel_message:
            status: Any = "cancelled"
        elif status_raw == "error" or event.exception is not None:
            status = "error"
        else:
            status = "success"
        duration_ms = int(event.duration * 1000) if event.duration is not None else None
        self._emitter.emit(
            ToolCallEndEvent(
                agent=self._agent_name,
                tool=tool_name,
                tool_use_id=tool_use_id,
                status=status,
                duration_ms=duration_ms,
                detail=event.cancel_message,
            )
        )


@dataclass
class PendingApproval:
    """Represents a suspended Two-Phase Commit intervention waiting for human approval."""

    approval_id: str
    session_id: str
    interrupt_id: str
    agent: str | None
    tool: str | None
    operation: str | None
    args: dict[str, Any]
    reason: str
    created_at: datetime
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at


def _parse_interrupt_metadata(interrupt: Interrupt, ttl_sec: int, session_id: str) -> PendingApproval:
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=ttl_sec)
    raw_reason = interrupt.reason
    agent_name: str | None = None
    tool_name: str | None = None
    operation: str | None = None
    args: dict[str, Any] = {}
    reason_text = str(raw_reason or "Destructive operation requires approval.")

    if isinstance(raw_reason, str) and raw_reason.strip().startswith("{"):
        try:
            parsed = json.loads(raw_reason)
            if isinstance(parsed, Mapping):
                agent_name = str(parsed.get("agent")) if parsed.get("agent") else None
                tool_name = str(parsed.get("tool")) if parsed.get("tool") else None
                operation = str(parsed.get("operation")) if parsed.get("operation") else None
                raw_args = parsed.get("args")
                if isinstance(raw_args, Mapping):
                    args = dict(raw_args)
                reason_text = str(parsed.get("reason") or reason_text)
        except json.JSONDecodeError:
            pass

    return PendingApproval(
        approval_id=uuid.uuid4().hex,
        session_id=session_id,
        interrupt_id=interrupt.id,
        agent=agent_name,
        tool=tool_name,
        operation=operation,
        args=args,
        reason=reason_text,
        created_at=now,
        expires_at=expires,
    )


@dataclass
class SessionOrchestrator:
    """Per-session Supervisor Agent instance with sliding-window memory and HITL state."""

    session_id: str
    agent: Agent
    emitter: SwitchableEmitter
    approval_timeout_sec: int
    keepalive_sec: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_approvals: dict[str, PendingApproval] = field(default_factory=dict)
    resolved_interrupt_responses: list[dict[str, Any]] = field(default_factory=list)
    last_active_monotonic: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_active_monotonic = time.monotonic()

    def prune_expired_approvals(self) -> list[PendingApproval]:
        expired = [p for p in self.pending_approvals.values() if p.is_expired]
        for item in expired:
            self.pending_approvals.pop(item.approval_id, None)
        return expired

    async def stream_prompt(self, prompt: str) -> AsyncIterator[StreamEvent]:
        """Run a user prompt through the Supervisor and yield structured SSE events."""
        self.prune_expired_approvals()
        if self.pending_approvals:
            pending_ids = list(self.pending_approvals.keys())
            raise PendingInterventionError(
                "Session has pending human-in-the-loop approval(s); approve or deny before sending a new prompt.",
                session_id=self.session_id,
                pending_approval_ids=pending_ids,
            )
        if self.lock.locked():
            raise SessionBusyError(
                f"Session '{self.session_id}' is already processing a request.",
                session_id=self.session_id,
            )
        async with self.lock:
            self.touch()
            async for ev in self._drive_agent_stream(prompt):
                yield ev

    async def stream_intervention_decision(
        self,
        approval_id: str,
        approved: bool,
        reason: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Resume a paused Supervisor turn with the user's Two-Phase Commit decision."""
        pending = self.pending_approvals.pop(approval_id, None)
        if pending is None:
            raise InterventionNotFoundError(
                f"Approval ID '{approval_id}' was not found or was already resolved.",
                approval_id=approval_id,
            )
        if self.lock.locked():
            # Restore pending before raising
            self.pending_approvals[approval_id] = pending
            raise SessionBusyError(
                f"Session '{self.session_id}' is currently busy.",
                session_id=self.session_id,
            )

        effective_approved = approved and not pending.is_expired
        response_item: dict[str, Any] = {
            "interruptResponse": {
                "interruptId": pending.interrupt_id,
                "response": {
                    "approved": effective_approved,
                    "reason": reason or ("expired" if pending.is_expired else None),
                },
            }
        }
        async with self.lock:
            self.touch()
            self.resolved_interrupt_responses.append(response_item)
            expired_siblings = self.prune_expired_approvals()
            for expired_item in expired_siblings:
                self.resolved_interrupt_responses.append(
                    {
                        "interruptResponse": {
                            "interruptId": expired_item.interrupt_id,
                            "response": {
                                "approved": False,
                                "reason": "expired",
                            },
                        }
                    }
                )

            if self.pending_approvals:
                yield SessionStartedEvent(session_id=self.session_id)
                yield InterventionResolvedEvent(
                    approval_id=approval_id,
                    approved=effective_approved,
                    expired=pending.is_expired,
                )
                yield DoneEvent(
                    session_id=self.session_id,
                    stop_reason="interrupt",
                    pending_approvals=list(self.pending_approvals.keys()),
                )
                return

            resume_payload = list(self.resolved_interrupt_responses)
            self.resolved_interrupt_responses.clear()
            yield InterventionResolvedEvent(
                approval_id=approval_id,
                approved=effective_approved,
                expired=pending.is_expired,
            )
            async for ev in self._drive_agent_stream(resume_payload):
                yield ev

    async def _drive_agent_stream(self, agent_input: Any) -> AsyncIterator[StreamEvent]:
        sink = QueueEventSink()
        self.emitter.attach(sink)
        supervisor_name = self.agent.name or "Supervisor"

        yield SessionStartedEvent(session_id=self.session_id)

        async def _producer() -> None:
            stop_reason = "end_turn"
            token = set_active_request_emitter(self.emitter)
            try:
                async for raw_event in self.agent.stream_async(agent_input):
                    if "data" in raw_event and isinstance(raw_event["data"], str):
                        text = raw_event["data"]
                        if text:
                            sink.emit(ContentDeltaEvent(agent=supervisor_name, text=text))
                    elif "result" in raw_event:
                        result = raw_event["result"]
                        stop_reason = str(getattr(result, "stop_reason", "end_turn"))
                        interrupts = getattr(result, "interrupts", None) or []
                        existing_interrupt_ids = {
                            p.interrupt_id for p in self.pending_approvals.values()
                        }
                        for intr in interrupts:
                            if intr.id in existing_interrupt_ids:
                                continue
                            existing_interrupt_ids.add(intr.id)
                            approval = _parse_interrupt_metadata(
                                intr,
                                ttl_sec=self.approval_timeout_sec,
                                session_id=self.session_id,
                            )
                            self.pending_approvals[approval.approval_id] = approval
                            sink.emit(
                                InterventionRequiredEvent(
                                    approval_id=approval.approval_id,
                                    agent=approval.agent,
                                    tool=approval.tool,
                                    operation=approval.operation,
                                    args=approval.args,
                                    reason=approval.reason,
                                    expires_at=approval.expires_at,
                                )
                            )
            except HomelabAgentError as exc:
                stop_reason = "error"
                sink.emit(ErrorEvent(code=exc.code, message=exc.message, details=exc.details))
            except Exception as exc:
                logger.exception("session=<%s> | supervisor stream failed", self.session_id)
                stop_reason = "error"
                sink.emit(ErrorEvent(code="internal_error", message=str(exc)))
            finally:
                reset_active_request_emitter(token)
                # Allow any async generator cleanup on sub-agents to release locks cleanly
                await asyncio.sleep(0)
                sink.emit(
                    DoneEvent(
                        session_id=self.session_id,
                        stop_reason=stop_reason,
                        pending_approvals=list(self.pending_approvals.keys()),
                    )
                )
                sink.close()

        producer_task = asyncio.create_task(_producer())
        try:
            while True:
                try:
                    item = await sink.get(timeout=self.keepalive_sec)
                except TimeoutError:
                    if producer_task.done():
                        break
                    continue
                if item is None:
                    break
                yield item  # type: ignore[misc]
        finally:
            self.emitter.detach(sink)
            if not producer_task.done():
                producer_task.cancel()
                try:
                    await producer_task
                except (asyncio.CancelledError, Exception):
                    pass


class SupervisorRuntime:
    """Manages session-scoped Supervisor instances with LRU + idle TTL eviction."""

    def __init__(
        self,
        config: AppConfig,
        factory: AgentFactory,
        memory_store: BaseMemoryStore,
    ) -> None:
        self._config = config
        self._factory = factory
        self._memory_store = memory_store
        self._sessions: OrderedDict[str, SessionOrchestrator] = OrderedDict()
        self._approval_index: dict[str, str] = {}

    def _evict_stale_sessions(self) -> None:
        now = time.monotonic()
        idle_ttl = self._config.sessions.idle_ttl_sec
        stale_ids = [
            sid
            for sid, sess in self._sessions.items()
            if not sess.lock.locked()
            and not sess.pending_approvals
            and (now - sess.last_active_monotonic) > idle_ttl
        ]
        for sid in stale_ids:
            self._sessions.pop(sid, None)

        max_sessions = self._config.sessions.max_sessions
        while len(self._sessions) >= max_sessions:
            evicted = False
            for sid, sess in list(self._sessions.items()):
                if not sess.lock.locked() and not sess.pending_approvals:
                    self._sessions.pop(sid, None)
                    evicted = True
                    break
            if not evicted:
                break

    def _build_session(self, session_id: str) -> SessionOrchestrator:
        emitter = SwitchableEmitter()
        sup_cfg = self._config.agents.supervisor
        sub_agent_tools = self._factory.build_sub_agent_tools(emitter=emitter)
        allowed_domains = {sub.domain for sub in self._config.agents.sub_agents.values()}
        record_tool = create_record_lesson_tool(
            store=self._memory_store,
            allowed_domains=allowed_domains,
            allow_core_invariants=self._config.memory.allow_agent_core_invariants,
            agent_min_occurrences=self._config.memory.agent_min_occurrences,
        )

        prompt = _SUPERVISOR_BASE_PROMPT
        if sup_cfg.extra_instructions.strip():
            prompt += f"\n\n## Site-Specific Supervisor Instructions\n{sup_cfg.extra_instructions.strip()}\n"

        conversation_manager = SlidingWindowConversationManager(
            window_size=sup_cfg.window_size,
            should_truncate_results=True,
        )
        hooks = SupervisorLifecycleHooks(agent_name=sup_cfg.name, emitter=emitter)
        gate = SafetyAndApprovalGate(agent_name=sup_cfg.name, config=self._config)

        supervisor_agent = Agent(
            name=sup_cfg.name,
            model=self._factory.build_model(sup_cfg.model_id, sup_cfg.temperature),
            system_prompt=prompt,
            tools=[*sub_agent_tools, record_tool],
            conversation_manager=conversation_manager,
            hooks=[hooks],
            interventions=[gate],
            retry_strategy=self._factory.build_retry_strategy(),
            tool_executor=self._factory.build_tool_executor(),
            callback_handler=None,
        )
        return SessionOrchestrator(
            session_id=session_id,
            agent=supervisor_agent,
            emitter=emitter,
            approval_timeout_sec=self._config.safety.approval_timeout_sec,
            keepalive_sec=self._config.server.sse_keepalive_sec,
        )

    def get_or_create_session(self, session_id: str | None = None) -> SessionOrchestrator:
        """Return an existing `SessionOrchestrator` or create a new one."""
        self._evict_stale_sessions()
        sid = (session_id or "").strip() or uuid.uuid4().hex
        session = self._sessions.get(sid)
        if session is not None:
            self._sessions.move_to_end(sid)
            return session
        session = self._build_session(sid)
        self._sessions[sid] = session
        return session

    def find_session_by_approval(self, approval_id: str) -> SessionOrchestrator:
        """Locate the session owning `approval_id` or raise `InterventionNotFoundError`."""
        sid = self._approval_index.get(approval_id)
        if sid and sid in self._sessions:
            return self._sessions[sid]
        for session in self._sessions.values():
            if approval_id in session.pending_approvals:
                self._approval_index[approval_id] = session.session_id
                return session
        raise InterventionNotFoundError(
            f"No pending intervention found for approval_id='{approval_id}'.",
            approval_id=approval_id,
        )

    def list_pending_approvals(self) -> list[PendingApproval]:
        items: list[PendingApproval] = []
        for session in self._sessions.values():
            session.prune_expired_approvals()
            for approval in session.pending_approvals.values():
                self._approval_index[approval.approval_id] = session.session_id
                items.append(approval)
        return items
