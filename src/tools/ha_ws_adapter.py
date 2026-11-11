"""Custom Home Assistant Async WebSocket Adapter & Deterministic State Asserter.

Provides sub-second sequential loops over a single authenticated WebSocket connection,
raw custom event firing on the HA event bus, REST fallback state queries, and
deterministic post-action state verification (`StateAssertionSuccess` / `StateAssertionFailure`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
import websockets
from strands import tool
from strands.types.tools import AgentTool
from websockets.asyncio.client import ClientConnection

from src.core.config import AppConfig
from src.core.exceptions import (
    HomeAssistantAuthError,
    HomeAssistantError,
    ResourceNotFoundError,
)
from src.core.guards import CircuitBreaker, GuardRails, HvacSetpointGuard, ServiceCallIntent
from src.core.interfaces import (
    BaseStateVerifier,
    EntityState,
    StateAssertionFailure,
    StateAssertionResult,
    StateAssertionSuccess,
)

logger = logging.getLogger(__name__)


class HomeAssistantWSAdapter(BaseStateVerifier):
    """Facade & Adapter over Home Assistant WebSocket and REST APIs."""

    def __init__(
        self,
        config: AppConfig,
        circuit_breaker: CircuitBreaker | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._ha_cfg = config.home_assistant
        self._guards = GuardRails(config.safety)
        self._hvac_guard = HvacSetpointGuard(config.safety, state_reader=self)
        self._breaker = circuit_breaker or CircuitBreaker(
            name="home_assistant",
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_sec=config.circuit_breaker.reset_timeout_sec,
        )
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._aliases = {k.strip().lower(): v.strip() for k, v in self._ha_cfg.entity_aliases.items()}

    def resolve_entity_id(self, entity_or_alias: str) -> str:
        """Resolve configured friendly aliases (e.g. 'living_room_tv') to canonical entity_ids."""
        cleaned = entity_or_alias.strip()
        return self._aliases.get(cleaned.lower(), cleaned)

    async def aclose(self) -> None:
        """Close owned HTTP client if initialized."""
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=self._config.ha_base_url,
                headers={
                    "Authorization": f"Bearer {self._config.ha_access_token.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                timeout=self._ha_cfg.rest_timeout_sec,
                verify=self._ha_cfg.verify_ssl,
            )
        return self._http_client

    def _ssl_context(self) -> ssl.SSLContext | bool | None:
        if not self._config.ha_ws_url.lower().startswith("wss://"):
            return None
        if self._ha_cfg.verify_ssl:
            return True
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    @asynccontextmanager
    async def _ws_session(self) -> AsyncIterator[ClientConnection]:
        """Open and authenticate a WebSocket connection to Home Assistant."""
        try:
            async with websockets.connect(
                self._config.ha_ws_url,
                open_timeout=self._ha_cfg.ws_open_timeout_sec,
                ssl=self._ssl_context(),
            ) as ws:
                raw_greeting = await asyncio.wait_for(ws.recv(), timeout=self._ha_cfg.ws_command_timeout_sec)
                greeting = json.loads(raw_greeting)
                if greeting.get("type") != "auth_required":
                    raise HomeAssistantError(f"Unexpected WS greeting from Home Assistant: {greeting}")

                await ws.send(
                    json.dumps(
                        {
                            "type": "auth",
                            "access_token": self._config.ha_access_token.get_secret_value(),
                        }
                    )
                )
                raw_auth = await asyncio.wait_for(ws.recv(), timeout=self._ha_cfg.ws_command_timeout_sec)
                auth_reply = json.loads(raw_auth)
                if auth_reply.get("type") != "auth_ok":
                    raise HomeAssistantAuthError(
                        "Home Assistant WebSocket authentication failed.",
                        detail=auth_reply.get("message", "auth_invalid"),
                    )
                yield ws
        except HomeAssistantError:
            raise
        except Exception as exc:
            raise HomeAssistantError(f"Home Assistant WebSocket connection error: {exc}") from exc

    async def _send_ws_command(
        self,
        ws: ClientConnection,
        msg_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        message = {"id": msg_id, **payload}
        await ws.send(json.dumps(message))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=self._ha_cfg.ws_command_timeout_sec)
            data: dict[str, Any] = json.loads(raw)
            if data.get("id") == msg_id and data.get("type") == "result":
                if not data.get("success", False):
                    err = data.get("error") or {}
                    raise HomeAssistantError(
                        f"HA WebSocket command '{payload.get('type')}' failed: {err.get('message', err)}",
                        ha_error=err,
                    )
                return data

    # ------------------------------------------------------------------
    # State reading (REST + WS fallback)
    # ------------------------------------------------------------------

    async def get_state(self, entity_id: str) -> EntityState | None:
        """Read current entity state via REST API."""
        canonical = self.resolve_entity_id(entity_id)
        async with self._breaker.guard():
            client = self._get_http_client()
            try:
                response = await client.get(f"/api/states/{canonical}")
            except httpx.HTTPError as exc:
                raise HomeAssistantError(f"Failed to query state for '{canonical}': {exc}") from exc
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                raise HomeAssistantError(
                    f"HA REST /api/states/{canonical} returned HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            body = response.json()
            return EntityState(
                entity_id=str(body.get("entity_id", canonical)),
                state=str(body.get("state", "")),
                attributes=dict(body.get("attributes") or {}),
            )

    # ------------------------------------------------------------------
    # Tool 0: Standard single service call with HVAC guard + state assertion
    # ------------------------------------------------------------------

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        service_data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a single Home Assistant service call over WebSocket with HVAC safety checks."""
        canonical = self.resolve_entity_id(entity_id)
        clean_domain = domain.strip().lower()
        clean_service = service.strip().lower()
        data = dict(service_data or {})

        intent = ServiceCallIntent(
            domain=clean_domain,
            service=clean_service,
            entity_ids=(canonical,),
            data=data,
        )
        await self._hvac_guard.validate_intent(intent)

        async with self._breaker.guard():
            async with self._ws_session() as ws:
                await self._send_ws_command(
                    ws,
                    1,
                    {
                        "type": "call_service",
                        "domain": clean_domain,
                        "service": clean_service,
                        "target": {"entity_id": canonical},
                        "service_data": data,
                    },
                )
        return {
            "status": "dispatched",
            "domain": clean_domain,
            "service": clean_service,
            "entity_id": canonical,
            "service_data": data,
        }

    # ------------------------------------------------------------------
    # Tool 1: Sub-second sequential loop over a single open WS connection
    # ------------------------------------------------------------------

    async def execute_sequential_loop(
        self,
        entity_id: str,
        domain: str,
        service: str,
        iterations: int,
        delay_ms: int,
        service_data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute rapid sequential service calls over a single persistent WebSocket session."""
        canonical = self.resolve_entity_id(entity_id)
        clean_domain = domain.strip().lower()
        clean_service = service.strip().lower()
        data = dict(service_data or {})

        self._guards.validate_loop(iterations=iterations, delay_ms=delay_ms)
        intent = ServiceCallIntent(
            domain=clean_domain,
            service=clean_service,
            entity_ids=(canonical,),
            data=data,
        )
        await self._hvac_guard.validate_intent(intent)

        timestamps_ms: list[int] = []
        started = time.monotonic()
        async with self._breaker.guard():
            async with self._ws_session() as ws:
                for idx in range(1, iterations + 1):
                    await self._send_ws_command(
                        ws,
                        idx,
                        {
                            "type": "call_service",
                            "domain": clean_domain,
                            "service": clean_service,
                            "target": {"entity_id": canonical},
                            "service_data": data,
                        },
                    )
                    timestamps_ms.append(int((time.monotonic() - started) * 1000))
                    if idx < iterations and delay_ms > 0:
                        await asyncio.sleep(delay_ms / 1000.0)

        final_state = await self.get_state(canonical)
        return {
            "entity_id": canonical,
            "domain": clean_domain,
            "service": clean_service,
            "iterations_completed": iterations,
            "delay_ms": delay_ms,
            "step_offsets_ms": timestamps_ms,
            "final_state": final_state.state if final_state else None,
        }

    # ------------------------------------------------------------------
    # Tool 2: Fire custom event on the HA event bus
    # ------------------------------------------------------------------

    async def fire_custom_ha_event(
        self,
        event_type: str,
        event_data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fire a custom event directly on the Home Assistant event bus (guarded by allow-list)."""
        clean_type = event_type.strip()
        self._guards.validate_event_type(clean_type)
        payload_data = dict(event_data or {})

        async with self._breaker.guard():
            async with self._ws_session() as ws:
                await self._send_ws_command(
                    ws,
                    1,
                    {
                        "type": "fire_event",
                        "event_type": clean_type,
                        "event_data": payload_data,
                    },
                )
        return {
            "fired": True,
            "event_type": clean_type,
            "event_data": payload_data,
        }

    # ------------------------------------------------------------------
    # Tool 3: Deterministic post-execution state assertion
    # ------------------------------------------------------------------

    def _matches(
        self,
        snapshot: EntityState,
        expected_state: str | None,
        expected_attributes: Mapping[str, Any],
    ) -> tuple[bool, str | None]:
        if expected_state is not None:
            if not self._value_matches(snapshot.state, expected_state):
                return (
                    False,
                    f"State mismatch for '{snapshot.entity_id}': expected '{expected_state}', "
                    f"actual '{snapshot.state}'.",
                )
        for attr_key, exp_val in expected_attributes.items():
            if attr_key not in snapshot.attributes:
                return (
                    False,
                    f"Attribute '{attr_key}' missing on '{snapshot.entity_id}'.",
                )
            act_val = snapshot.attributes.get(attr_key)
            if not self._value_matches(act_val, exp_val):
                return (
                    False,
                    f"Attribute '{attr_key}' mismatch on '{snapshot.entity_id}': "
                    f"expected '{exp_val}', actual '{act_val}'.",
                )
        return True, None

    def _value_matches(self, actual: Any, expected: Any) -> bool:
        try:
            act_num = float(actual)
            exp_num = float(expected)
            return abs(act_num - exp_num) <= self._ha_cfg.numeric_tolerance
        except (TypeError, ValueError):
            return str(actual).strip().lower() == str(expected).strip().lower()

    async def assert_state(
        self,
        entity_id: str,
        expected_state: str | None,
        timeout_sec: float | None = None,
        poll_interval_ms: int | None = None,
        expected_attributes: Mapping[str, Any] | None = None,
    ) -> StateAssertionResult:
        """Verify physical/system execution via polling + WebSocket state_changed subscription."""
        canonical = self.resolve_entity_id(entity_id)
        eff_timeout = timeout_sec if timeout_sec is not None else self._ha_cfg.default_assert_timeout_sec
        eff_poll_ms = poll_interval_ms if poll_interval_ms is not None else self._ha_cfg.default_poll_interval_ms
        exp_attrs = dict(expected_attributes or {})

        self._guards.validate_timeout(eff_timeout)
        started = time.monotonic()
        deadline = started + eff_timeout
        last_snapshot: EntityState | None = None
        last_reason: str | None = None

        # 1. Immediate check before waiting
        initial = await self.get_state(canonical)
        if initial is None:
            raise ResourceNotFoundError(
                f"Entity '{canonical}' does not exist in Home Assistant.",
                entity_id=canonical,
            )
        last_snapshot = initial
        matched, last_reason = self._matches(initial, expected_state, exp_attrs)
        if matched:
            return StateAssertionSuccess(
                entity_id=canonical,
                expected_state=expected_state,
                expected_attributes=exp_attrs,
                actual_state=initial.state,
                actual_attributes=initial.attributes,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                method="polling",
            )

        # 2. Poll at poll_interval_ms until deadline
        interval_sec = max(0.05, eff_poll_ms / 1000.0)
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            await asyncio.sleep(min(interval_sec, max(0.0, remaining)))
            current = await self.get_state(canonical)
            if current is None:
                continue
            last_snapshot = current
            matched, last_reason = self._matches(current, expected_state, exp_attrs)
            if matched:
                return StateAssertionSuccess(
                    entity_id=canonical,
                    expected_state=expected_state,
                    expected_attributes=exp_attrs,
                    actual_state=current.state,
                    actual_attributes=current.attributes,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    method="polling",
                )

        return StateAssertionFailure(
            entity_id=canonical,
            expected_state=expected_state,
            expected_attributes=exp_attrs,
            actual_state=last_snapshot.state if last_snapshot else None,
            actual_attributes=last_snapshot.attributes if last_snapshot else {},
            elapsed_ms=int((time.monotonic() - started) * 1000),
            method="polling",
            reason=last_reason or f"Entity '{canonical}' did not reach '{expected_state}' within {eff_timeout}s.",
        )

    # ------------------------------------------------------------------
    # Strands AgentTool builders
    # ------------------------------------------------------------------

    def build_tools(self) -> list[AgentTool]:
        """Return the Strands tools backed by this adapter."""
        adapter = self

        @tool(
            name="get_ha_entity_state",
            description="Read the current state and attributes of a Home Assistant entity.",
        )
        async def get_ha_entity_state(entity_id: str) -> dict[str, Any]:
            """Query a Home Assistant entity's live state and attributes.

            Args:
                entity_id: Canonical entity_id (e.g. 'climate.main_thermostat') or configured alias.
            """
            state = await adapter.get_state(entity_id)
            if state is None:
                raise ResourceNotFoundError(
                    f"Entity '{adapter.resolve_entity_id(entity_id)}' was not found.",
                    entity_id=entity_id,
                )
            return state.model_dump(mode="json")

        @tool(
            name="call_ha_service",
            description=(
                "Call a Home Assistant service on an entity (e.g. turn_on, turn_off, set_temperature). "
                "IMPORTANT: After calling any state-changing service, you MUST call assert_entity_state "
                "to verify the physical state change before reporting success."
            ),
        )
        async def call_ha_service(
            domain: str,
            service: str,
            entity_id: str,
            service_data: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Call a Home Assistant service over WebSocket with HVAC & window-open safety guards.

            Args:
                domain: Home Assistant domain (e.g. 'light', 'climate', 'media_player').
                service: Service name (e.g. 'turn_on', 'turn_off', 'set_temperature').
                entity_id: Target entity_id or alias.
                service_data: Optional service parameters (e.g. {'temperature': 21.5}).
            """
            return await adapter.call_service(
                domain=domain,
                service=service,
                entity_id=entity_id,
                service_data=service_data,
            )

        @tool(
            name="execute_sequential_loop",
            description=(
                "Execute rapid sequential actions on a Home Assistant entity over a single open "
                "WebSocket connection (e.g. toggle a light 3 times with 800ms intervals). "
                "Follow up with assert_entity_state to confirm the final state."
            ),
        )
        async def execute_sequential_loop(
            entity_id: str,
            domain: str,
            service: str,
            iterations: int,
            delay_ms: int,
            service_data: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Run a fast sequential service loop over a single WebSocket connection.

            Args:
                entity_id: Target Home Assistant entity_id or configured alias.
                domain: Home Assistant domain (e.g. 'light', 'switch').
                service: Service to invoke on each iteration (e.g. 'toggle').
                iterations: Number of iterations to execute (bounded by safety.max_loop_iterations).
                delay_ms: Delay in milliseconds between iterations.
                service_data: Optional extra payload for the service call.
            """
            return await adapter.execute_sequential_loop(
                entity_id=entity_id,
                domain=domain,
                service=service,
                iterations=iterations,
                delay_ms=delay_ms,
                service_data=service_data,
            )

        @tool(
            name="fire_custom_ha_event",
            description="Fire a custom event directly on the Home Assistant event bus (must match allowed patterns).",
        )
        async def fire_custom_ha_event(
            event_type: str,
            event_data: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Fire an allow-listed custom event on the Home Assistant event bus.

            Args:
                event_type: Event name matching safety.allowed_custom_event_patterns.
                event_data: JSON-serializable event payload dictionary.
            """
            return await adapter.fire_custom_ha_event(event_type=event_type, event_data=event_data)

        @tool(
            name="assert_entity_state",
            description=(
                "Deterministic Actor-Critic state verifier: polls Home Assistant until the entity "
                "reaches expected_state (and optional expected_attributes) or timeout_sec expires. "
                "Returns StateAssertionSuccess or StateAssertionFailure."
            ),
        )
        async def assert_entity_state(
            entity_id: str,
            expected_state: str | None = None,
            timeout_sec: float | None = None,
            poll_interval_ms: int | None = None,
            expected_attributes: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Verify that an entity physically reached the expected state after an action.

            Args:
                entity_id: Entity ID or alias to verify.
                expected_state: Expected state string (e.g. 'on', 'off', 'heat', 'playing').
                timeout_sec: Maximum seconds to wait before returning StateAssertionFailure.
                poll_interval_ms: Polling interval in milliseconds.
                expected_attributes: Optional attribute key/values to assert (e.g. {'temperature': 21.5}).
            """
            result = await adapter.assert_state(
                entity_id=entity_id,
                expected_state=expected_state,
                timeout_sec=timeout_sec,
                poll_interval_ms=poll_interval_ms,
                expected_attributes=expected_attributes,
            )
            return result.model_dump(mode="json")

        return [
            get_ha_entity_state,
            call_ha_service,
            execute_sequential_loop,
            fire_custom_ha_event,
            assert_entity_state,
        ]
