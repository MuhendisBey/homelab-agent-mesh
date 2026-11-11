"""FastAPI SSE Streaming Gateway (`/api/v1/chat/stream` & `/api/v1/interventions/approve`)."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src import __version__
from src.agents.supervisor import SessionOrchestrator, SupervisorRuntime
from src.api.schemas import (
    ChatStreamRequest,
    ErrorEvent,
    HealthResponse,
    InterventionDecisionRequest,
    LessonCreateRequest,
    LessonResponse,
    PendingApprovalSummary,
)
from src.core.config import AppConfig
from src.core.exceptions import (
    HomelabAgentError,
    InterventionNotFoundError,
    PendingInterventionError,
    SessionBusyError,
)
from src.memory.base import BaseMemoryStore

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
) -> StreamingResponse:
    """Stream structured JSON SSE packets (`plan_step`, `tool_call_start`, `state_assertion`,
    `intervention_required`, `content_delta`, `done`) for a user prompt.
    """
    session: SessionOrchestrator = runtime.get_or_create_session(body.session_id)
    if session.prune_expired_approvals() and session.pending_approvals:
        pass
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

    stream = session.stream_prompt(body.prompt)
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

