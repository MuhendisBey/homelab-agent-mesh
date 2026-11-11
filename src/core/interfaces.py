"""Abstract interfaces (Dependency Inversion) shared across layers.

Agents, hooks and guards depend on these protocols - never on concrete adapters -
so every backend can be replaced by a mock in unit tests or dry-run mode.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class EntityState(BaseModel):
    """Minimal Home Assistant entity snapshot."""

    entity_id: str
    state: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class StateAssertionResult(BaseModel):
    """Outcome of a post-execution state assertion (Actor-Critic evidence)."""

    assertion: Literal["StateAssertionSuccess", "StateAssertionFailure"]
    verified: bool
    entity_id: str
    expected_state: str | None = None
    expected_attributes: dict[str, Any] = Field(default_factory=dict)
    actual_state: str | None = None
    actual_attributes: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: int
    method: Literal["subscription", "polling"]
    reason: str | None = None


class StateAssertionSuccess(StateAssertionResult):
    """The entity reached the expected state within the timeout."""

    assertion: Literal["StateAssertionSuccess"] = "StateAssertionSuccess"
    verified: Literal[True] = True


class StateAssertionFailure(StateAssertionResult):
    """The entity did NOT reach the expected state - the agent must report the truth."""

    assertion: Literal["StateAssertionFailure"] = "StateAssertionFailure"
    verified: Literal[False] = False


class BaseStateVerifier(ABC):
    """Reads physical state and asserts post-conditions after an action."""

    @abstractmethod
    async def get_state(self, entity_id: str) -> EntityState | None:
        """Return the current entity state or ``None`` if the entity does not exist."""

    @abstractmethod
    async def assert_state(
        self,
        entity_id: str,
        expected_state: str | None,
        timeout_sec: float,
        poll_interval_ms: int,
        expected_attributes: Mapping[str, Any] | None = None,
    ) -> StateAssertionResult:
        """Wait until the entity matches the expectation or the timeout expires."""


@runtime_checkable
class EventEmitter(Protocol):
    """Sink for structured lifecycle events (bridged to SSE by the API layer)."""

    def emit(self, event: BaseModel) -> None:
        """Publish an event. Must never raise and must be safe to call when nobody listens."""
        ...

