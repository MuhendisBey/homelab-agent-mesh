"""Pre-condition guard clauses, approval policy and circuit breaker.

Guards are deliberately framework-agnostic (pure Python) so they are enforced twice:

* inside the native tools themselves (defense in depth), and
* by the Strands intervention layer for *any* tool - including external ha-mcp tools
  that bypass our adapters (see ``src/agents/hooks.py``).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from src.core.config import SafetyBounds
from src.core.exceptions import (
    CircuitOpenError,
    DisallowedEventError,
    GuardViolationError,
    HomelabAgentError,
    ProtectedResourceViolationError,
    SafetyBoundViolationError,
    WindowOpenProtectionError,
)
from src.core.interfaces import BaseStateVerifier

logger = logging.getLogger(__name__)

# Home Assistant semantics (protocol constants, not environment data).
_CLIMATE_DOMAIN = "climate"
_SETPOINT_SERVICES = frozenset({"set_temperature"})
_HEATING_ACTIVATION_SERVICES = frozenset({"set_temperature", "turn_on", "set_hvac_mode", "set_preset_mode"})
_SETPOINT_KEYS = ("temperature", "target_temp_low", "target_temp_high")
_SERVICE_DATA_KEYS = ("data", "service_data", "parameters", "params")
_OPEN_SENSOR_STATES = frozenset({"on", "open"})
_HVAC_OFF = "off"


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitState(StrEnum):
    """Classic three-state circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Short-circuits calls to a failing backend to prevent retry storms / infinite loops.

    Guard violations and "not found" errors are *caller* errors and are not counted as
    backend failures.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int,
        reset_timeout_sec: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout_sec
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        """Current state, lazily transitioning OPEN -> HALF_OPEN after the cooldown."""
        if self._opened_at is None:
            return CircuitState.CLOSED
        if self._clock() - self._opened_at >= self._reset_timeout:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def before_call(self) -> None:
        """Raise :class:`CircuitOpenError` while the circuit is open."""
        if self.state is CircuitState.OPEN:
            assert self._opened_at is not None
            remaining = self._reset_timeout - (self._clock() - self._opened_at)
            raise CircuitOpenError(
                f"Circuit '{self.name}' is open after {self._failures} consecutive failures; "
                f"retry in {remaining:.0f}s.",
                backend=self.name,
                retry_after_sec=round(remaining, 1),
            )

    def record_success(self) -> None:
        """Close the circuit."""
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        """Count a failure; open the circuit when the threshold is reached (or a probe fails)."""
        self._failures += 1
        if self.state is CircuitState.HALF_OPEN or self._failures >= self._failure_threshold:
            self._opened_at = self._clock()
            logger.warning("circuit=<%s> failures=<%d> | circuit opened", self.name, self._failures)

    @asynccontextmanager
    async def guard(self) -> AsyncIterator[None]:
        """Async context manager wrapping one backend interaction."""
        self.before_call()
        try:
            yield
        except (GuardViolationError, CircuitOpenError):
            raise
        except HomelabAgentError as exc:
            if exc.code == "resource_not_found":
                raise
            self.record_failure()
            raise
        except Exception:
            self.record_failure()
            raise
        else:
            self.record_success()


# ---------------------------------------------------------------------------
# Service-call inspection (works for native AND external MCP tool arguments)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceCallIntent:
    """Normalized view of a Home Assistant service call found in arbitrary tool args."""

    domain: str
    service: str
    entity_ids: tuple[str, ...]
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def setpoints(self) -> list[float]:
        """All requested temperature setpoints (``temperature`` / ``target_temp_*``)."""
        values: list[float] = []
        for key in _SETPOINT_KEYS:
            raw = self.data.get(key)
            if raw is None:
                continue
            try:
                values.append(float(raw))
            except (TypeError, ValueError) as exc:
                raise SafetyBoundViolationError(f"Non-numeric setpoint '{raw}' for '{key}'.") from exc
        return values


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def _collect_entity_ids(*sources: Mapping[str, Any] | None) -> tuple[str, ...]:
    found: list[str] = []
    for source in sources:
        if not source:
            continue
        for candidate in (source.get("entity_id"), (_as_mapping(source.get("target")) or {}).get("entity_id")):
            if isinstance(candidate, str):
                found.extend(part.strip() for part in candidate.split(",") if part.strip())
            elif isinstance(candidate, Iterable):
                found.extend(str(item) for item in candidate)
    return tuple(dict.fromkeys(found))


def extract_service_call(args: Mapping[str, Any]) -> ServiceCallIntent | None:
    """Best-effort extraction of a service call from tool arguments of any shape."""
    domain = args.get("domain")
    service = args.get("service") or args.get("action")
    if isinstance(service, str) and "." in service and not isinstance(domain, str):
        domain, service = service.split(".", 1)
    if not isinstance(domain, str) or not isinstance(service, str):
        return None
    data: dict[str, Any] = {}
    for key in _SERVICE_DATA_KEYS:
        mapping = _as_mapping(args.get(key))
        if mapping:
            data.update(mapping)
    entity_ids = _collect_entity_ids(args, data)
    return ServiceCallIntent(domain=domain, service=service, entity_ids=entity_ids, data=data)


# ---------------------------------------------------------------------------
# Domain guards
# ---------------------------------------------------------------------------


class HvacSetpointGuard:
    """Validates heating changes against bounds and window-open frost protection."""

    def __init__(self, bounds: SafetyBounds, state_reader: BaseStateVerifier | None) -> None:
        self._bounds = bounds
        self._reader = state_reader
        self._zones = {zone.climate_entity: zone for zone in bounds.climate_zones}

    def validate_bounds(self, setpoints: Iterable[float]) -> None:
        """Raise when any setpoint is outside ``[min_hvac_temp, max_hvac_temp]``."""
        for value in setpoints:
            if not self._bounds.min_hvac_temp <= value <= self._bounds.max_hvac_temp:
                raise SafetyBoundViolationError(
                    f"Setpoint {value}°C is outside the allowed range "
                    f"[{self._bounds.min_hvac_temp}, {self._bounds.max_hvac_temp}]°C.",
                    requested=value,
                    min=self._bounds.min_hvac_temp,
                    max=self._bounds.max_hvac_temp,
                )

    async def validate_intent(self, intent: ServiceCallIntent) -> None:
        """Validate a climate service call (no-op for other domains)."""
        if intent.domain != _CLIMATE_DOMAIN or intent.service not in _HEATING_ACTIVATION_SERVICES:
            return
        if intent.service == "set_hvac_mode" and str(intent.data.get("hvac_mode", "")).lower() == _HVAC_OFF:
            return  # switching heating off is always safe
        if intent.service in _SETPOINT_SERVICES:
            self.validate_bounds(intent.setpoints)
        for entity_id in intent.entity_ids:
            await self._ensure_no_window_protection(entity_id)

    async def _ensure_no_window_protection(self, climate_entity: str) -> None:
        if self._reader is None:
            raise GuardViolationError(
                "Cannot verify window-open protection: no state reader configured (failing closed).",
                entity_id=climate_entity,
            )
        zone = self._zones.get(climate_entity)
        try:
            if zone:
                for sensor in zone.window_sensors:
                    sensor_state = await self._reader.get_state(sensor)
                    if sensor_state and sensor_state.state.lower() in _OPEN_SENSOR_STATES:
                        raise WindowOpenProtectionError(
                            f"Window sensor '{sensor}' is open; heating changes on '{climate_entity}' are "
                            "blocked to respect the window-open automation.",
                            entity_id=climate_entity,
                            window_sensor=sensor,
                        )
            current = await self._reader.get_state(climate_entity)
        except GuardViolationError:
            raise
        except Exception as exc:  # backend down -> fail closed
            raise GuardViolationError(
                f"Cannot read state needed for the window-open guard ({exc}); failing closed.",
                entity_id=climate_entity,
            ) from exc
        if current is None:
            return
        setpoint = current.attributes.get("temperature")
        try:
            current_setpoint = float(setpoint) if setpoint is not None else None
        except (TypeError, ValueError):
            current_setpoint = None
        if current_setpoint is not None and current_setpoint <= self._bounds.window_open_guard_temp:
            raise WindowOpenProtectionError(
                f"'{climate_entity}' is at {current_setpoint}°C (<= window-open guard "
                f"{self._bounds.window_open_guard_temp}°C): frost/window protection is active, refusing to override.",
                entity_id=climate_entity,
                current_setpoint=current_setpoint,
            )


class GuardRails:
    """Stateless guard clauses for loops, events and protected infrastructure resources."""

    def __init__(self, bounds: SafetyBounds) -> None:
        self._bounds = bounds
        self._protected_containers = {name.lstrip("/").lower() for name in bounds.protected_containers}
        self._protected_container_actions = {a.lower() for a in bounds.protected_container_actions}
        self._protected_guest_actions = {a.lower() for a in bounds.protected_guest_actions}

    def validate_loop(self, iterations: int, delay_ms: int) -> None:
        """Bound sequential-loop parameters to prevent runaway actuator toggling."""
        if not 1 <= iterations <= self._bounds.max_loop_iterations:
            raise SafetyBoundViolationError(
                f"iterations={iterations} must be between 1 and {self._bounds.max_loop_iterations}."
            )
        if not self._bounds.min_loop_delay_ms <= delay_ms <= self._bounds.max_loop_delay_ms:
            raise SafetyBoundViolationError(
                f"delay_ms={delay_ms} must be between {self._bounds.min_loop_delay_ms} and "
                f"{self._bounds.max_loop_delay_ms}."
            )

    def validate_timeout(self, timeout_sec: float) -> None:
        """Bound assertion timeouts."""
        if not 0 < timeout_sec <= self._bounds.max_assert_timeout_sec:
            raise SafetyBoundViolationError(
                f"timeout_sec={timeout_sec} must be in (0, {self._bounds.max_assert_timeout_sec}]."
            )

    def validate_event_type(self, event_type: str) -> None:
        """Only allow-listed custom event types may be fired on the HA bus."""
        patterns = self._bounds.allowed_custom_event_patterns
        if not any(fnmatch.fnmatchcase(event_type, pattern) for pattern in patterns):
            raise DisallowedEventError(
                f"Event type '{event_type}' is not allowed. Allowed patterns: {patterns or 'none'}.",
                event_type=event_type,
            )

    def is_protected_container(self, container_name: str) -> bool:
        """True when the container is on the protected list."""
        return container_name.lstrip("/").lower() in self._protected_containers

    def ensure_container_action_allowed(self, container_name: str, action: str) -> None:
        """Raise :class:`ProtectedResourceViolationError` for guarded actions on protected containers."""
        if action.lower() in self._protected_container_actions and self.is_protected_container(container_name):
            raise ProtectedResourceViolationError(
                f"Container '{container_name}' is protected; action '{action}' is forbidden.",
                container=container_name,
                action=action,
            )

    def ensure_guest_action_allowed(self, vmid: int, action: str) -> None:
        """Raise :class:`ProtectedResourceViolationError` for guarded actions on protected VMs/LXCs."""
        if action.lower() in self._protected_guest_actions and vmid in self._bounds.protected_vm_ids:
            raise ProtectedResourceViolationError(
                f"Guest {vmid} is protected; action '{action}' is forbidden.", vmid=vmid, action=action
            )


# ---------------------------------------------------------------------------
# Approval policy (Command pattern: tool call -> named operation)
# ---------------------------------------------------------------------------

OperationResolver = Callable[[Mapping[str, Any]], str]


class OperationRegistry:
    """Maps a tool call to a named *operation* used by the human-approval policy.

    Tools register resolvers (Open/Closed): e.g. ``manage_container(action="restart")``
    resolves to ``restart_container``. Unregistered tools resolve to their own name.
    """

    def __init__(self) -> None:
        self._resolvers: dict[str, OperationResolver] = {}

    def register(self, tool_name: str, resolver: OperationResolver) -> None:
        """Register (or replace) the resolver for ``tool_name``."""
        self._resolvers[tool_name] = resolver

    def resolve(self, tool_name: str, args: Mapping[str, Any]) -> str:
        """Return the operation name for a concrete tool call."""
        resolver = self._resolvers.get(tool_name)
        if resolver is None:
            return tool_name
        try:
            return resolver(args)
        except Exception:  # malformed args -> fall back to the strictest identity
            logger.warning("tool=<%s> | operation resolver failed; using tool name", tool_name)
            return tool_name

    def requires_approval(self, tool_name: str, args: Mapping[str, Any], destructive: Iterable[str]) -> str | None:
        """Return the matched operation name when approval is required, else ``None``."""
        destructive_set = set(destructive)
        operation = self.resolve(tool_name, args)
        if operation in destructive_set:
            return operation
        if tool_name in destructive_set:
            return tool_name
        return None


operation_registry = OperationRegistry()
"""Process-wide registry populated by tool modules at import time."""

