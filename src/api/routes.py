"""FastAPI SSE Streaming Gateway (`/api/v1/chat/stream` & `/api/v1/interventions/approve`)."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src import __version__
from src.agents.supervisor import SessionOrchestrator, SupervisorRuntime
from src.api.schemas import (
    AnnouncementResult,
    ChatStreamRequest,
    EntityMemoryCreateRequest,
    EntityMemoryResponse,
    EntityRefreshResponse,
    EntityResolveResponse,
    ErrorEvent,
    HealthResponse,
    InterventionDecisionRequest,
    LessonCreateRequest,
    LessonResponse,
    PendingApprovalSummary,
    ScheduleCreateRequest,
    ScheduleResponse,
    VoiceAnnounceRequest,
    VoiceCommandRequest,
    VoiceCommandResponse,
)
from src.core.announcer import Announcer, clean_for_tts, prepare_prompt_context
from src.core.config import AppConfig
from src.core.entity_resolver import EntityResolver
from src.core.events import (
    ContentDeltaEvent,
    DoneEvent,
    InterventionRequiredEvent,
    ScheduleCreatedEvent,
    StateAssertionEvent,
)
from src.core.exceptions import (
    GuardViolationError,
    HomelabAgentError,
    InterventionNotFoundError,
    PendingInterventionError,
    ResourceNotFoundError,
    SessionBusyError,
)
from src.core.scheduler import ActionScheduler, ScheduleStatus
from src.memory.base import BaseMemoryStore
from src.tools.ha_ws_adapter import HomeAssistantWSAdapter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["orchestrator"])


def get_app_config(request: Request) -> AppConfig:
    cfg: AppConfig = request.app.state.config
    return cfg


def get_supervisor_runtime(request: Request) -> SupervisorRuntime:
    runtime: SupervisorRuntime = request.app.state.runtime
    return runtime


def get_memory_store(request: Request) -> BaseMemoryStore:
    store: BaseMemoryStore = request.app.state.memory_store
    return store


def get_entity_resolver(request: Request) -> EntityResolver:
    resolver: EntityResolver = request.app.state.entity_resolver
    return resolver


def get_scheduler(request: Request) -> ActionScheduler:
    scheduler: ActionScheduler = request.app.state.scheduler
    return scheduler


def get_announcer(request: Request) -> Announcer:
    announcer: Announcer = request.app.state.announcer
    return announcer


def get_ha_adapter(request: Request) -> HomeAssistantWSAdapter | None:
    ha_adapter: HomeAssistantWSAdapter | None = getattr(request.app.state, "ha_adapter", None)
    return ha_adapter


def _acquire_ready_session(
    runtime: SupervisorRuntime,
    session_id: str | None,
) -> SessionOrchestrator:
    session: SessionOrchestrator = runtime.get_or_create_session(session_id)
    session.prune_expired_approvals()
    if session.pending_approvals:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": PendingInterventionError.code,
                "message": "Session is paused awaiting human approval.",
                "session_id": session.session_id,
                "pending_approval_ids": list(session.pending_approvals.keys()),
            },
        )
    if session.lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": SessionBusyError.code,
                "message": f"Session '{session.session_id}' is already processing a request.",
            },
        )
    return session


def _acquire_voice_session(
    runtime: SupervisorRuntime,
    session_id: str | None,
) -> SessionOrchestrator:
    """Acquire a session for voice without blocking concurrent turns on background work."""
    session: SessionOrchestrator = runtime.get_or_create_session(session_id)
    session.prune_expired_approvals()
    if session.pending_approvals:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": PendingInterventionError.code,
                "message": "Session is paused awaiting human approval.",
                "session_id": session.session_id,
                "pending_approval_ids": list(session.pending_approvals.keys()),
            },
        )
    if session.lock.locked():
        overflow_id = f"{session.session_id}__bg_{uuid.uuid4().hex[:8]}"
        logger.info(
            "voice | session '%s' busy; routed concurrent voice turn to '%s'",
            session.session_id,
            overflow_id,
        )
        return runtime.get_or_create_session(overflow_id)
    return session


async def verify_bearer_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """Enforce optional bearer token auth when `API_BEARER_TOKEN` is set in `.env`."""
    cfg = get_app_config(request)
    if cfg.api_bearer_token is None:
        return
    expected = cfg.api_bearer_token.get_secret_value().strip()
    if not expected:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token.")
    provided = authorization[7:].strip()
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Bearer token.")


def _format_sse(event: BaseModel) -> str:
    payload = event.model_dump_json()
    return f"data: {payload}\n\n"


async def _sse_event_generator(stream: AsyncIterator[BaseModel]) -> AsyncIterator[str]:
    try:
        async for ev in stream:
            yield _format_sse(ev)
    except HomelabAgentError as exc:
        yield _format_sse(ErrorEvent(code=exc.code, message=exc.message, details=exc.details))


@router.get("/health", response_model=HealthResponse)
async def health_check(
    config: AppConfig = Depends(get_app_config),
    runtime: SupervisorRuntime = Depends(get_supervisor_runtime),
) -> HealthResponse:
    """Lightweight liveness/readiness probe."""
    enabled_agents = [
        sub.tool_name for sub in config.agents.sub_agents.values() if sub.enabled
    ]
    return HealthResponse(
        status="ok",
        version=__version__,
        active_sub_agents=enabled_agents,
        pending_interventions=len(runtime.list_pending_approvals()),
    )


@router.post(
    "/chat/stream",
    dependencies=[Depends(verify_bearer_token)],
    response_class=StreamingResponse,
)
async def chat_stream(
    body: ChatStreamRequest,
    runtime: SupervisorRuntime = Depends(get_supervisor_runtime),
    config: AppConfig = Depends(get_app_config),
    entity_resolver: EntityResolver = Depends(get_entity_resolver),
    ha_adapter: HomeAssistantWSAdapter | None = Depends(get_ha_adapter),
) -> StreamingResponse:
    """Stream structured JSON SSE packets (`plan_step`, `tool_call_start`, `state_assertion`,
    `intervention_required`, `content_delta`, `done`) for a user prompt.
    """
    session = _acquire_ready_session(runtime, body.session_id)
    prepared = await prepare_prompt_context(
        text=body.prompt,
        config=config,
        entity_resolver=entity_resolver,
        ha_adapter=ha_adapter,
        room=body.room,
        response_style=body.response_style,
    )

    stream = session.stream_prompt(prepared.augmented_prompt)
    return StreamingResponse(
        _sse_event_generator(stream),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-ID": session.session_id,
        },
    )


@router.get(
    "/interventions/pending",
    dependencies=[Depends(verify_bearer_token)],
    response_model=list[PendingApprovalSummary],
)
async def list_pending_interventions(
    runtime: SupervisorRuntime = Depends(get_supervisor_runtime),
) -> list[PendingApprovalSummary]:
    """List all active Two-Phase Commit approval tokens."""
    approvals = runtime.list_pending_approvals()
    return [
        PendingApprovalSummary(
            approval_id=a.approval_id,
            session_id=a.session_id,
            agent=a.agent,
            tool=a.tool,
            operation=a.operation,
            args=a.args,
            reason=a.reason,
            created_at=a.created_at,
            expires_at=a.expires_at,
        )
        for a in approvals
    ]


@router.post(
    "/interventions/approve",
    dependencies=[Depends(verify_bearer_token)],
    response_class=StreamingResponse,
)
async def approve_or_deny_intervention(
    body: InterventionDecisionRequest,
    runtime: SupervisorRuntime = Depends(get_supervisor_runtime),
) -> StreamingResponse:
    """Resume a paused Supervisor session after human approval or rejection and stream the remaining turn."""
    try:
        session = (
            runtime.get_or_create_session(body.session_id)
            if body.session_id
            else runtime.find_session_by_approval(body.approval_id)
        )
    except InterventionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.to_dict()) from exc

    if body.approval_id not in session.pending_approvals:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": InterventionNotFoundError.code,
                "message": f"Approval ID '{body.approval_id}' not found on session '{session.session_id}'.",
            },
        )

    stream = session.stream_intervention_decision(
        approval_id=body.approval_id,
        approved=body.approved,
        reason=body.reason,
    )
    return StreamingResponse(
        _sse_event_generator(stream),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-ID": session.session_id,
        },
    )


@router.post(
    "/memory/lessons",
    dependencies=[Depends(verify_bearer_token)],
    response_model=LessonResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_lesson(
    body: LessonCreateRequest,
    store: BaseMemoryStore = Depends(get_memory_store),
) -> LessonResponse:
    """Directly insert a Core Invariant or domain rule into the 3-Tier Reflection Store."""
    record = await store.record_lesson(
        domain=body.domain,
        rule_content=body.rule_content,
        is_core_invariant=body.is_core_invariant,
    )
    return LessonResponse(
        id=record.id,
        domain=record.domain,
        rule_content=record.rule_content,
        is_core_invariant=record.is_core_invariant,
        created=record.created,
    )


@router.get(
    "/memory/lessons",
    dependencies=[Depends(verify_bearer_token)],
    response_model=list[str],
)
async def query_lessons(
    domain: str = Query(min_length=1, max_length=64),
    query: str = Query(default="", max_length=1000),
    store: BaseMemoryStore = Depends(get_memory_store),
) -> list[str]:
    """Retrieve matching Core Invariants and semantic rules for a domain and query."""
    return await store.get_relevant_rules(domain=domain, query=query)


# ---------------------------------------------------------------------------
# P1: EntityResolver & CoALA Entity Memory routes
# ---------------------------------------------------------------------------


@router.get(
    "/entities/resolve",
    dependencies=[Depends(verify_bearer_token)],
    response_model=EntityResolveResponse,
)
async def resolve_entities(
    q: str = Query(min_length=1, max_length=500),
    domain: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=5, ge=1, le=20),
    resolver: EntityResolver = Depends(get_entity_resolver),
) -> EntityResolveResponse:
    """Resolve a natural-language Turkish/English entity reference in <10ms."""
    return await resolver.resolve(query=q, domain_hint=domain, limit=limit)


@router.post(
    "/entities/refresh",
    dependencies=[Depends(verify_bearer_token)],
    response_model=EntityRefreshResponse,
)
async def refresh_entity_registry(
    resolver: EntityResolver = Depends(get_entity_resolver),
) -> EntityRefreshResponse:
    """Force an immediate synchronization of the HA Grounding Index & entity_memory."""
    count = await resolver.refresh()
    return EntityRefreshResponse(
        status="ok",
        entity_count=count,
        last_sync_utc=resolver.last_sync_utc,
    )


@router.get(
    "/entities/memory",
    dependencies=[Depends(verify_bearer_token)],
    response_model=list[EntityMemoryResponse],
)
async def list_entity_memory(
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    resolver: EntityResolver = Depends(get_entity_resolver),
) -> list[EntityMemoryResponse]:
    """List learned phrase -> entity_id mappings from CoALA semantic memory."""
    return await resolver.list_memory(status=status_filter)


@router.post(
    "/entities/memory",
    dependencies=[Depends(verify_bearer_token)],
    response_model=EntityMemoryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upsert_entity_memory(
    body: EntityMemoryCreateRequest,
    resolver: EntityResolver = Depends(get_entity_resolver),
) -> EntityMemoryResponse:
    """Teach or correct a phrase -> entity_id mapping in CoALA entity_memory."""
    if body.is_correction or body.wrong_entity_id:
        saved = await resolver.record_user_correction(
            phrase=body.phrase,
            correct_entity_id=body.entity_id,
            wrong_entity_id=body.wrong_entity_id,
        )
    else:
        saved = await resolver.record_verified_resolution(
            phrase=body.phrase,
            entity_id=body.entity_id,
            source=body.source,
        )
    if saved is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "invalid_entity_mapping",
                "message": f"Could not persist mapping '{body.phrase}' -> '{body.entity_id}'.",
            },
        )
    return saved


# ---------------------------------------------------------------------------
# P2: Persistent Action Scheduler routes
# ---------------------------------------------------------------------------


@router.post(
    "/schedules",
    dependencies=[Depends(verify_bearer_token)],
    response_model=ScheduleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_schedule(
    body: ScheduleCreateRequest,
    scheduler: ActionScheduler = Depends(get_scheduler),
) -> ScheduleResponse:
    """Create a persistent deferred Home Assistant action."""
    try:
        return await scheduler.schedule_action(
            domain=body.domain,
            service=body.service,
            entity_id=body.entity_id,
            delay_seconds=body.delay_seconds,
            run_at_utc=body.run_at_utc,
            service_data=body.service_data,
            expected_state=body.expected_state,
            expected_attributes=body.expected_attributes,
            description=body.description,
            misfire_policy=body.misfire_policy,
            misfire_grace_sec=body.misfire_grace_sec,
            created_by="api",
        )
    except GuardViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.to_dict(),
        ) from exc


@router.get(
    "/schedules",
    dependencies=[Depends(verify_bearer_token)],
    response_model=list[ScheduleResponse],
)
async def list_schedules(
    status_filter: ScheduleStatus | None = Query(default=None, alias="status"),
    entity_id: str | None = Query(default=None, max_length=256),
    scheduler: ActionScheduler = Depends(get_scheduler),
) -> list[ScheduleResponse]:
    """List scheduled Home Assistant actions filtered by optional status or entity_id."""
    return await scheduler.list_actions(status=status_filter, entity_id=entity_id)


@router.delete(
    "/schedules/{schedule_id}",
    dependencies=[Depends(verify_bearer_token)],
    response_model=ScheduleResponse,
)
async def cancel_schedule(
    schedule_id: str,
    scheduler: ActionScheduler = Depends(get_scheduler),
) -> ScheduleResponse:
    """Cancel a pending scheduled Home Assistant action."""
    try:
        return await scheduler.cancel_action(schedule_id)
    except ResourceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.to_dict()) from exc
    except GuardViolationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.to_dict()) from exc


# ---------------------------------------------------------------------------
# P4: Voice Gateway (/voice) & HomePod AirPlay TTS Announcer
# ---------------------------------------------------------------------------


@dataclass
class _VoiceTurnOutcome:
    status: Literal["completed", "approval_required", "error"]
    speech: str
    verified_entities: list[str]
    scheduled_ids: list[str]
    pending_approvals: list[str]


async def _consume_voice_turn(
    session: SessionOrchestrator,
    prompt: str,
) -> _VoiceTurnOutcome:
    text_parts: list[str] = []
    verified_entities: list[str] = []
    scheduled_ids: list[str] = []
    pending_approvals: list[str] = []
    error_messages: list[str] = []
    stop_reason = "end_turn"

    try:
        async for ev in session.stream_prompt(prompt):
            if isinstance(ev, ContentDeltaEvent):
                text_parts.append(ev.text)
            elif isinstance(ev, StateAssertionEvent):
                if ev.verified and ev.entity and ev.entity not in verified_entities:
                    verified_entities.append(ev.entity)
            elif isinstance(ev, ScheduleCreatedEvent):
                if ev.schedule_id and ev.schedule_id not in scheduled_ids:
                    scheduled_ids.append(ev.schedule_id)
            elif isinstance(ev, InterventionRequiredEvent):
                if ev.approval_id and ev.approval_id not in pending_approvals:
                    pending_approvals.append(ev.approval_id)
            elif isinstance(ev, ErrorEvent):
                if ev.message:
                    error_messages.append(ev.message)
            elif isinstance(ev, DoneEvent):
                stop_reason = ev.stop_reason
                for approval_id in ev.pending_approvals:
                    if approval_id and approval_id not in pending_approvals:
                        pending_approvals.append(approval_id)
    except HomelabAgentError as exc:
        error_messages.append(exc.message)
        stop_reason = "error"

    speech = clean_for_tts("".join(text_parts))
    if pending_approvals or stop_reason == "interrupt":
        turn_status: Literal["completed", "approval_required", "error"] = "approval_required"
        if not speech:
            speech = "Bu işlem yıkıcı etki içeriyor ve insan onayı bekliyor."
    elif error_messages or stop_reason == "error":
        turn_status = "error"
        if not speech:
            speech = clean_for_tts(error_messages[-1]) if error_messages else "Komut işlenirken bir hata oluştu."
    else:
        turn_status = "completed"
        if not speech:
            speech = "İşlem tamamlandı."

    return _VoiceTurnOutcome(
        status=turn_status,
        speech=speech,
        verified_entities=verified_entities,
        scheduled_ids=scheduled_ids,
        pending_approvals=pending_approvals,
    )


async def _announce_when_done(
    task: asyncio.Task[_VoiceTurnOutcome],
    announcer: Announcer,
    room: str | None,
    speaker_entity: str | None,
) -> None:
    try:
        outcome = await task
        await announcer.announce(
            outcome.speech,
            room=room,
            speaker_entity=speaker_entity,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("voice | background announcement watcher failed")


def _track_background_task(request: Request, task: asyncio.Task[Any]) -> None:
    voice_tasks: set[asyncio.Task[Any]] | None = getattr(request.app.state, "voice_tasks", None)
    if voice_tasks is None:
        voice_tasks = set()
        request.app.state.voice_tasks = voice_tasks
    voice_tasks.add(task)
    task.add_done_callback(voice_tasks.discard)


@router.post(
    "/voice",
    dependencies=[Depends(verify_bearer_token)],
    response_model=VoiceCommandResponse,
)
async def voice_command(
    body: VoiceCommandRequest,
    request: Request,
    runtime: SupervisorRuntime = Depends(get_supervisor_runtime),
    config: AppConfig = Depends(get_app_config),
    announcer: Announcer = Depends(get_announcer),
    entity_resolver: EntityResolver = Depends(get_entity_resolver),
    ha_adapter: HomeAssistantWSAdapter | None = Depends(get_ha_adapter),
) -> VoiceCommandResponse:
    """Execute a spoken smart-home command within the synchronous voice timeout budget.

    If execution exceeds `timeout_sec` (default 8s), returns immediately with
    `status='accepted_background'` while allowing the agent turn to finish and announce
    its result asynchronously via `Announcer`.
    """
    started_at = time.monotonic()
    prepared = await prepare_prompt_context(
        text=body.text,
        config=config,
        entity_resolver=entity_resolver,
        ha_adapter=ha_adapter,
        room=body.room,
        response_style="voice",
    )
    session = _acquire_voice_session(runtime, body.session_id)
    cleaned_prompt = prepared.cleaned_prompt
    wake_detected = prepared.wake_word_detected

    bg_task = asyncio.create_task(
        _consume_voice_turn(session=session, prompt=prepared.augmented_prompt)
    )
    _track_background_task(request, bg_task)

    effective_timeout = body.timeout_sec or config.voice.sync_timeout_sec
    done, _ = await asyncio.wait({bg_task}, timeout=effective_timeout)

    if bg_task in done:
        try:
            outcome = bg_task.result()
        except Exception as exc:
            logger.exception("voice | turn failed unexpectedly")
            outcome = _VoiceTurnOutcome(
                status="error",
                speech=clean_for_tts(str(exc)) or "Komut işlenirken beklenmeyen bir hata oluştu.",
                verified_entities=[],
                scheduled_ids=[],
                pending_approvals=[],
            )
        announcement: AnnouncementResult | None = None
        if body.announce:
            announcement = await announcer.announce(
                outcome.speech,
                room=body.room,
                speaker_entity=body.speaker_entity,
            )
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            "voice | session=%s status=%s elapsed_ms=%d timed_out=False",
            session.session_id,
            outcome.status,
            elapsed_ms,
        )
        return VoiceCommandResponse(
            status=outcome.status,
            speech=outcome.speech,
            raw_text=body.text,
            cleaned_prompt=cleaned_prompt,
            wake_word_detected=wake_detected,
            session_id=session.session_id,
            timed_out=False,
            elapsed_ms=elapsed_ms,
            verified_entities=outcome.verified_entities,
            scheduled_ids=outcome.scheduled_ids,
            pending_approvals=outcome.pending_approvals,
            announcement=announcement,
        )

    watcher_task = asyncio.create_task(
        _announce_when_done(
            task=bg_task,
            announcer=announcer,
            room=body.room,
            speaker_entity=body.speaker_entity,
        )
    )
    _track_background_task(request, watcher_task)
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    logger.info(
        "voice | session=%s status=accepted_background elapsed_ms=%d timed_out=True",
        session.session_id,
        elapsed_ms,
    )
    return VoiceCommandResponse(
        status="accepted_background",
        speech=config.voice.deferred_ack_text,
        raw_text=body.text,
        cleaned_prompt=cleaned_prompt,
        wake_word_detected=wake_detected,
        session_id=session.session_id,
        timed_out=True,
        elapsed_ms=elapsed_ms,
    )


@router.post(
    "/voice/announce",
    dependencies=[Depends(verify_bearer_token)],
    response_model=AnnouncementResult,
)
async def voice_announce(
    body: VoiceAnnounceRequest,
    announcer: Announcer = Depends(get_announcer),
) -> AnnouncementResult:
    """Directly dispatch a clean TTS announcement to a room HomePod or target speaker."""
    return await announcer.announce(
        message=body.message,
        room=body.room,
        speaker_entity=body.speaker_entity,
        language=body.language,
    )

