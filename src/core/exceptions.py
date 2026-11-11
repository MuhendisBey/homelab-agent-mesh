"""Domain, safety and circuit-breaker exceptions.

Every exception carries a machine-readable ``code`` so it can be rendered into a
structured tool result / SSE ``error`` event instead of a raw traceback.
"""

from __future__ import annotations

from typing import Any


class HomelabAgentError(Exception):
    """Base class for all orchestrator errors."""

    code: str = "homelab_agent_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def to_dict(self) -> dict[str, Any]:
        """Structured representation suitable for tool results and SSE payloads."""
        return {"error": self.code, "message": self.message, **self.details}


# --- Configuration ---------------------------------------------------------------------


class ConfigurationError(HomelabAgentError):
    """Invalid or missing configuration detected at runtime."""

    code = "configuration_error"


# --- Guard clauses / safety ------------------------------------------------------------


class GuardViolationError(HomelabAgentError):
    """A pre-condition guard clause rejected the requested operation."""

    code = "guard_violation"


class SafetyBoundViolationError(GuardViolationError):
    """A parameter is outside the configured safety bounds (e.g. HVAC setpoint)."""

    code = "safety_bound_violation"


class WindowOpenProtectionError(GuardViolationError):
    """A heating change was requested while window-open frost protection is active."""

    code = "window_open_protection"


class ProtectedResourceViolationError(GuardViolationError):
    """A destructive action targeted a protected container / VM."""

    code = "protected_resource_violation"


class DisallowedEventError(GuardViolationError):
    """A custom event type is not on the configured allow-list."""

    code = "disallowed_event"


# --- Circuit breaker -------------------------------------------------------------------


class CircuitOpenError(HomelabAgentError):
    """The backend circuit is open; calls are short-circuited until the cooldown expires."""

    code = "circuit_open"


class ToolBudgetExceededError(HomelabAgentError):
    """An agent exceeded its tool-call budget or retried an identical call too often."""

    code = "tool_budget_exceeded"


# --- Backends --------------------------------------------------------------------------


class BackendError(HomelabAgentError):
    """Base class for external backend failures."""

    code = "backend_error"


class HomeAssistantError(BackendError):
    """Home Assistant returned an error or was unreachable."""

    code = "home_assistant_error"


class HomeAssistantAuthError(HomeAssistantError):
    """WebSocket authentication with Home Assistant failed."""

    code = "home_assistant_auth_error"


class DockerBackendError(BackendError):
    """Docker daemon call failed."""

    code = "docker_error"


class ProxmoxBackendError(BackendError):
    """Proxmox VE API call failed."""

    code = "proxmox_error"


class ResourceNotFoundError(BackendError):
    """The referenced entity / container / guest does not exist."""

    code = "resource_not_found"


# --- Sessions / interventions ----------------------------------------------------------


class SessionBusyError(HomelabAgentError):
    """A request is already running for this session."""

    code = "session_busy"


class SessionNotFoundError(HomelabAgentError):
    """Unknown or expired session id."""

    code = "session_not_found"


class PendingInterventionError(HomelabAgentError):
    """The session is suspended waiting for a human decision."""

    code = "pending_intervention"


class InterventionNotFoundError(HomelabAgentError):
    """Unknown approval id."""

    code = "intervention_not_found"

