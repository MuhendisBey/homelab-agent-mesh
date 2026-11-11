"""Pydantic request/response and SSE event schemas for the FastAPI gateway."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from src.core.events import (
    ContentDeltaEvent,
    CriticVerdictEvent,
    DoneEvent,
    ErrorEvent,
    InterventionRequiredEvent,
    InterventionResolvedEvent,
    MemoryInjectedEvent,
    PlanStepEvent,
    SessionStartedEvent,
    StateAssertionEvent,
    StreamEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)

__all__ = [
    "ChatStreamRequest",
    "ContentDeltaEvent",
    "CriticVerdictEvent",
    "DoneEvent",
    "ErrorEvent",
    "HealthResponse",
    "InterventionDecisionRequest",
    "InterventionRequiredEvent",
    "InterventionResolvedEvent",
    "LessonCreateRequest",
    "LessonResponse",
    "MemoryInjectedEvent",
    "PendingApprovalSummary",
    "PlanStepEvent",
    "SessionStartedEvent",
    "StateAssertionEvent",
    "StreamEvent",
    "ToolCallEndEvent",
    "ToolCallStartEvent",
]


class ChatStreamRequest(BaseModel):
    """Request body for `POST /api/v1/chat/stream`."""

    prompt: str = Field(min_length=1, max_length=16000, description="User instruction or multi-intent prompt.")
    session_id: str | None = Field(
        default=None,
        max_length=128,
        description="Optional conversation session ID; generated automatically when omitted.",
    )


class InterventionDecisionRequest(BaseModel):
    """Request body for `POST /api/v1/interventions/approve`."""

    approval_id: str = Field(min_length=1, max_length=128, description="Approval token emitted in intervention_required.")
    approved: bool = Field(description="True to approve and execute the paused tool call; False to reject.")
    session_id: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class PendingApprovalSummary(BaseModel):
    """Summary of an active Two-Phase Commit approval token."""

    approval_id: str
    session_id: str
    agent: str | None = None
    tool: str | None = None
    operation: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str
    created_at: datetime
    expires_at: datetime


class LessonCreateRequest(BaseModel):
    """Request body for `POST /api/v1/memory/lessons`."""

    domain: str = Field(min_length=1, max_length=64, description="Bounded context ('ha' or 'infra').")
    rule_content: str = Field(min_length=3, max_length=2000)
    is_core_invariant: bool = False


class LessonResponse(BaseModel):
    """Response model for a persisted reflection lesson."""

    id: int
    domain: str
    rule_content: str
    is_core_invariant: bool
    created: bool


class HealthResponse(BaseModel):
    """Response model for `GET /api/v1/health`."""

    status: str = "ok"
    version: str
    active_sub_agents: list[str]
    pending_interventions: int

