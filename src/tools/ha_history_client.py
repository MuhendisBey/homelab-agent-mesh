"""Async Home Assistant History Reader & Virtual Sensor Publisher.

Handles:
- Fetching all entity states (delegating to ``HomeAssistantWSAdapter`` when attached).
- Fetching ``/api/history/period/{start}`` with automatic batch splitting so
  ``climate.*`` and ``weather.*`` entities never have their attributes stripped by
  Home Assistant's ``minimal_response`` / ``no_attributes`` API quirk.
- Publishing thermal twin virtual sensors (``sensor.thermal_twin_*``) and advisor
  reports via ``POST /api/states/{entity_id}`` protected by ``CircuitBreaker``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from src.core.config import AppConfig
from src.core.exceptions import HomeAssistantError
from src.core.guards import CircuitBreaker
from src.core.thermal_engine import ThermalSnapshotSummary, ZoneLiveTelemetry
from src.tools.ha_ws_adapter import HomeAssistantWSAdapter

logger = logging.getLogger(__name__)

_MAX_HA_STATE_LEN = 245
_MAX_ADVISOR_STATE_LEN = 240
_MINIMAL_HISTORY_DOMAINS = ("sensor.", "binary_sensor.")


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _format_ha_timestamp(dt: datetime) -> str:
    return _to_utc(dt).isoformat().replace("+00:00", "Z")


def _serialize_timestamp(value: datetime | str | None) -> str | None:
    if isinstance(value, datetime):
        return _to_utc(value).isoformat()
    return value


def _split_history_entities(entity_ids: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split entity IDs into attribute-rich vs minimal-response batches."""
    rich_ids: list[str] = []
    minimal_ids: list[str] = []
    seen: set[str] = set()
    for raw_eid in entity_ids:
        eid = raw_eid.strip()
        if not eid or eid in seen:
            continue
        seen.add(eid)
        if eid.lower().startswith(_MINIMAL_HISTORY_DOMAINS):
            minimal_ids.append(eid)
        else:
            rich_ids.append(eid)
    return rich_ids, minimal_ids


def _normalize_history_payload(
    payload: Any,
    requested_ids: Sequence[str],
) -> dict[str, list[dict[str, Any]]]:
    """Normalize HA ``/api/history/period`` response into ``{entity_id: [state_dicts]}``."""
    normalized: dict[str, list[dict[str, Any]]] = {eid: [] for eid in requested_ids}
    if isinstance(payload, Mapping):
        for eid, entries in payload.items():
            if not isinstance(entries, list):
                continue
            bucket = normalized.setdefault(str(eid), [])
            for entry in entries:
                if isinstance(entry, Mapping):
                    entry_dict = dict(entry)
                    entry_dict.setdefault("entity_id", str(eid))
                    bucket.append(entry_dict)
        return normalized

    if not isinstance(payload, list):
        return normalized

    for item in payload:
        if isinstance(item, list):
            series_eid = next(
                (
                    str(entry["entity_id"]).strip()
                    for entry in item
                    if isinstance(entry, Mapping) and entry.get("entity_id")
                ),
                None,
            )
            for entry in item:
                if not isinstance(entry, Mapping):
                    continue
                entry_dict = dict(entry)
                eid = str(entry_dict.get("entity_id") or series_eid or "").strip()
                if not eid:
                    continue
                entry_dict.setdefault("entity_id", eid)
                normalized.setdefault(eid, []).append(entry_dict)
        elif isinstance(item, Mapping):
            entry_dict = dict(item)
            eid = str(entry_dict.get("entity_id") or "").strip()
            if eid:
                normalized.setdefault(eid, []).append(entry_dict)
    return normalized


class HAHistoryClient:
    """Async Home Assistant REST History & Virtual Sensor client."""

    def __init__(
        self,
        config: AppConfig,
        ha_adapter: HomeAssistantWSAdapter | None = None,
        http_client: httpx.AsyncClient | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self._config = config
        self._ha_adapter = ha_adapter
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._breaker = circuit_breaker or CircuitBreaker(
            name="home_assistant_history",
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_sec=config.circuit_breaker.reset_timeout_sec,
        )

    async def aclose(self) -> None:
        """Close owned HTTP client if initialized."""
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is not None:
            return self._http_client
        if self._ha_adapter is not None:
            adapter_client = getattr(self._ha_adapter, "_http_client", None)
            if isinstance(adapter_client, httpx.AsyncClient):
                return adapter_client
        ha_cfg = self._config.home_assistant
        self._http_client = httpx.AsyncClient(
            base_url=self._config.ha_base_url,
            headers={
                "Authorization": f"Bearer {self._config.ha_access_token.get_secret_value()}",
                "Content-Type": "application/json",
            },
            timeout=ha_cfg.rest_timeout_sec,
            verify=ha_cfg.verify_ssl,
        )
        return self._http_client

    def _build_url(self, client: httpx.AsyncClient, path: str) -> str:
        if str(client.base_url):
            return path
        return f"{self._config.ha_base_url}{path}"

    async def fetch_all_states(self) -> list[dict[str, Any]]:
        """Fetch all current Home Assistant states."""
        if self._ha_adapter is not None:
            return await self._ha_adapter.get_all_states()

        async with self._breaker.guard():
            client = self._get_http_client()
            try:
                response = await client.get(self._build_url(client, "/api/states"))
            except httpx.HTTPError as exc:
                raise HomeAssistantError(f"Failed to query /api/states: {exc}") from exc
            if response.status_code >= 400:
                raise HomeAssistantError(
                    f"HA REST /api/states returned HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            data = response.json() if response.content else []
            if not isinstance(data, list):
                return []
            return [dict(item) for item in data if isinstance(item, Mapping)]

    async def _fetch_history_batch(
        self,
        entity_ids: Sequence[str],
        start_iso: str,
        end_iso: str | None,
        minimal: bool,
    ) -> dict[str, list[dict[str, Any]]]:
        params: dict[str, str] = {"filter_entity_id": ",".join(entity_ids)}
        if end_iso is not None:
            params["end_time"] = end_iso
        if minimal:
            params["minimal_response"] = "1"
            params["no_attributes"] = "1"

        path = f"/api/history/period/{start_iso}"
        async with self._breaker.guard():
            client = self._get_http_client()
            try:
                response = await client.get(self._build_url(client, path), params=params)
            except httpx.HTTPError as exc:
                raise HomeAssistantError(f"Failed to query {path}: {exc}") from exc
            if response.status_code >= 400:
                raise HomeAssistantError(
                    f"HA REST {path} returned HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            payload = response.json() if response.content else []
            return _normalize_history_payload(payload, entity_ids)

    async def fetch_history(
        self,
        entity_ids: Sequence[str],
        start_utc: datetime,
        end_utc: datetime | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch historical state changes without stripping climate/weather attributes."""
        rich_ids, minimal_ids = _split_history_entities(entity_ids)
        if not rich_ids and not minimal_ids:
            return {}

        start_iso = _format_ha_timestamp(start_utc)
        end_iso = _format_ha_timestamp(end_utc) if end_utc is not None else None

        combined: dict[str, list[dict[str, Any]]] = {}
        if rich_ids:
            combined.update(
                await self._fetch_history_batch(
                    entity_ids=rich_ids,
                    start_iso=start_iso,
                    end_iso=end_iso,
                    minimal=False,
                )
            )
        if minimal_ids:
            combined.update(
                await self._fetch_history_batch(
                    entity_ids=minimal_ids,
                    start_iso=start_iso,
                    end_iso=end_iso,
                    minimal=True,
                )
            )
        return combined

    async def publish_virtual_sensor(
        self,
        entity_id: str,
        state: str | float,
        attributes: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Publish or update a virtual sensor entity via ``POST /api/states/{entity_id}``."""
        canonical = entity_id.strip()
        payload: dict[str, Any] = {
            "state": str(state)[:_MAX_HA_STATE_LEN],
            "attributes": dict(attributes),
        }
        path = f"/api/states/{canonical}"
        async with self._breaker.guard():
            client = self._get_http_client()
            try:
                response = await client.post(self._build_url(client, path), json=payload)
            except httpx.HTTPError as exc:
                raise HomeAssistantError(
                    f"Failed to publish virtual sensor '{canonical}': {exc}"
                ) from exc
            if response.status_code >= 400:
                raise HomeAssistantError(
                    f"HA REST POST {path} returned HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            body = response.json() if response.content else {}
            return dict(body) if isinstance(body, Mapping) else {}

    async def _publish_zone_sensors(self, zone: ZoneLiveTelemetry) -> list[str]:
        published: list[str] = []
        ts_utc = _serialize_timestamp(zone.timestamp_utc)

        true_eid = f"sensor.thermal_twin_{zone.zone_id}_true_temp"
        true_state: str | float = (
            round(zone.true_ambient_temp_c, 2) if zone.true_ambient_temp_c is not None else "unknown"
        )
        true_attrs: dict[str, Any] = {
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
            "friendly_name": f"{zone.name} Gerçek Sıcaklık (Termal İkiz)",
            "zone_id": zone.zone_id,
            "raw_etrv_temp_c": zone.raw_etrv_temp_c,
            "etrv_corrected_temp_c": zone.etrv_corrected_temp_c,
            "target_setpoint_c": zone.target_setpoint_c,
            "valve_position_pct": zone.valve_position_pct,
            "spatial_gradient_c": zone.spatial_gradient_c,
            "etrv_bias_factor": zone.etrv_bias_factor,
            "static_offset_c": zone.static_offset_c,
            "hvac_action": zone.hvac_action,
            "bosch_state": zone.bosch_state,
            "timestamp_utc": ts_utc,
        }
        await self.publish_virtual_sensor(
            entity_id=true_eid,
            state=true_state,
            attributes=true_attrs,
        )
        published.append(true_eid)

        stats_eid = f"sensor.thermal_twin_{zone.zone_id}_stats"
        stats_state: str | float = (
            round(zone.cooling_rate_c_per_h, 3)
            if zone.cooling_rate_c_per_h is not None
            else "monitoring"
        )
        stats_attrs: dict[str, Any] = {
            "friendly_name": f"{zone.name} Termal İstatistikler",
            "zone_id": zone.zone_id,
            "volume_m3": zone.volume_m3,
            "k_loss": zone.k_loss,
            "tau_hours": zone.tau_hours,
            "cooling_rate_c_per_h": zone.cooling_rate_c_per_h,
            "warmup_rate_c_per_h": zone.warmup_rate_c_per_h,
            "night_min_temp_c": zone.night_min_temp_c,
            "optimal_ventilation_min": zone.optimal_ventilation_min,
            "rel_humidity_pct": zone.rel_humidity_pct,
            "absolute_humidity_g_m3": zone.absolute_humidity_g_m3,
            "dew_point_c": zone.dew_point_c,
            "enthalpy_kj_kg": zone.enthalpy_kj_kg,
            "mold_risk": zone.mold_risk,
            "window_open": zone.window_open,
            "frost_protection_active": zone.frost_protection_active,
            "anomaly_detected": zone.anomaly_detected,
            "anomaly_reason": zone.anomaly_reason,
            "spatial_gradient_c": zone.spatial_gradient_c,
            "timestamp_utc": ts_utc,
        }
        if zone.cooling_rate_c_per_h is not None:
            stats_attrs["unit_of_measurement"] = "°C/h"
        await self.publish_virtual_sensor(
            entity_id=stats_eid,
            state=stats_state,
            attributes=stats_attrs,
        )
        published.append(stats_eid)
        return published

    async def publish_snapshot_sensors(self, snapshot: ThermalSnapshotSummary) -> list[str]:
        """Publish per-zone true temperature and thermal statistics virtual sensors."""
        published: list[str] = []
        for zone in snapshot.zones.values():
            published.extend(await self._publish_zone_sensors(zone))
        return published

    async def publish_advisor_report(
        self,
        summary_state: str,
        report_markdown: str,
        extra_attributes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Publish the thermal twin Markdown advisor report sensor to Home Assistant."""
        attributes: dict[str, Any] = {
            "friendly_name": "Termal İkiz Danışman Raporu",
            "icon": "mdi:home-thermometer-outline",
            "report_markdown": report_markdown,
            "updated_at_utc": datetime.now(UTC).isoformat(),
        }
        if extra_attributes:
            attributes.update(dict(extra_attributes))

        return await self.publish_virtual_sensor(
            entity_id=self._config.thermal.advisor_report_entity,
            state=summary_state[:_MAX_ADVISOR_STATE_LEN],
            attributes=attributes,
        )
