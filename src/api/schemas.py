"""Pydantic request/response and SSE event schemas for the FastAPI gateway."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from src.core.entity_resolver import (
    EntityCandidate,
    EntityMemoryRecord,
    EntityResolutionResult,
)
from src.core.events import (
    ContentDeltaEvent,
    CriticVerdictEvent,
    DoneEvent,
    ErrorEvent,
    InterventionRequiredEvent,
    InterventionResolvedEvent,
    MemoryInjectedEvent,
    PlanStepEvent,
    ScheduleCreatedEvent,
    ScheduleFiredEvent,
    ScheduleResultEvent,
    SessionStartedEvent,
    StateAssertionEvent,
    StreamEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from src.core.scheduler import MisfirePolicy, ScheduledActionRecord

__all__ = [
    "ChatStreamRequest",
    "ContentDeltaEvent",
    "CriticVerdictEvent",
    "DoneEvent",
    "EntityCandidate",
    "EntityMemoryCreateRequest",
    "EntityMemoryResponse",
    "EntityRefreshResponse",
    "EntityResolveResponse",
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
    "ScheduleCreateRequest",
    "ScheduleCreatedEvent",
    "ScheduleFiredEvent",
    "ScheduleResponse",
    "ScheduleResultEvent",
    "SessionStartedEvent",
    "StateAssertionEvent",
    "StreamEvent",
    "ToolCallEndEvent",
    "ToolCallStartEvent",
]

EntityResolveResponse = EntityResolutionResult
EntityMemoryResponse = EntityMemoryRecord
ScheduleResponse = ScheduledActionRecord


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


class EntityRefreshResponse(BaseModel):
    """Response model for `POST /api/v1/entities/refresh`."""

    status: str = "ok"
    entity_count: int
    last_sync_utc: str | None = None


class EntityMemoryCreateRequest(BaseModel):
    """Request body for `POST /api/v1/entities/memory`."""

    phrase: str = Field(min_length=1, max_length=256)
    entity_id: str = Field(min_length=3, max_length=256)
    wrong_entity_id: str | None = Field(default=None, max_length=256)
    is_correction: bool = False
    source: str = Field(default="user_correction", max_length=64)


class ScheduleCreateRequest(BaseModel):
    """Request body for `POST /api/v1/schedules`."""

    domain: str = Field(min_length=1, max_length=64)
    service: str = Field(min_length=1, max_length=64)
    entity_id: str = Field(min_length=3, max_length=256)
    delay_seconds: int | None = Field(default=None, ge=1, le=86400)
    run_at_utc: datetime | None = None
    service_data: dict[str, Any] = Field(default_factory=dict)
    expected_state: str | None = None
    expected_attributes: dict[str, Any] = Field(default_factory=dict)
    description: str = Field(default="", max_length=500)
    misfire_policy: MisfirePolicy = "safe_direction"
    misfire_grace_sec: int = Field(default=300, ge=0, le=86400)

    @model_validator(mode="after")
    def _validate_timing(self) -> ScheduleCreateRequest:
        if self.delay_seconds is None and self.run_at_utc is None:
            raise ValueError("Either delay_seconds or run_at_utc must be provided.")
        return self

