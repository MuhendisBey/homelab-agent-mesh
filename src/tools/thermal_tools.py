"""Thermal Twin Service lifecycle orchestrator and Strands AgentTool definitions."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from strands import tool
from strands.types.tools import AgentTool

from src.core.config import AppConfig
from src.core.entity_resolver import EntityResolver, RegistryEntityRecord
from src.core.exceptions import HomeAssistantError
from src.core.neonplan_adapter import BuildingTopology, NeonPlanAdapter
from src.core.thermal_engine import ThermalPhysicsEngine, ThermalSnapshotSummary
from src.memory.thermal_store import ThermalAdvisorReportRecord, ThermalStore
from src.tools.ha_history_client import HAHistoryClient
from src.tools.ha_ws_adapter import HomeAssistantWSAdapter

logger = logging.getLogger(__name__)

_ANOMALY_DEBOUNCE_SEC = 3600.0
_MAX_LOOKBACK_HOURS = 168


def _dedupe_str(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in items:
        item = raw.strip()
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _parse_hhmm(raw: str) -> tuple[int, int]:
    hour_s, min_s = raw.strip().split(":", 1)
    return int(hour_s), int(min_s)


def _format_deterministic_briefing(
    snapshot: ThermalSnapshotSummary,
    history_summary: Mapping[str, Any],
    simulation: Mapping[str, Any],
) -> tuple[str, str]:
    """Render a deterministic Markdown briefing when no LLM advisor runner is attached."""
    lines: list[str] = [
        f"## Günlük Termal İkiz Özeti ({snapshot.timestamp_utc})",
        "",
    ]
    if snapshot.outdoor_temp_c is not None:
        hum_part = (
            f" | Dış Nem: %{snapshot.outdoor_humidity_pct:.0f}"
            if snapshot.outdoor_humidity_pct is not None
            else ""
        )
        lines.append(f"- **Dış Ortam:** {snapshot.outdoor_temp_c:.1f} °C{hum_part}")

    lines.append(f"- **İzlenen Zon Sayısı:** {snapshot.monitored_zone_count}")
    if snapshot.anomaly_zones:
        lines.append(f"- **Anomali Tespit Edilen Zonlar:** {', '.join(snapshot.anomaly_zones)}")
    if snapshot.high_mold_risk_zones:
        lines.append(f"- **Yüksek Küf Riski:** {', '.join(snapshot.high_mold_risk_zones)}")
    if snapshot.open_window_zones:
        lines.append(f"- **Açık Pencere / Donma Koruması:** {', '.join(snapshot.open_window_zones)}")

    lines.extend(["", "### Oda Bazlı Durum"])
    projections = simulation.get("projections")
    proj_map = projections if isinstance(projections, Mapping) else {}
    for zone in snapshot.zones.values():
        t_true = f"{zone.true_ambient_temp_c:.2f} °C" if zone.true_ambient_temp_c is not None else "—"
        t_etrv = f"{zone.raw_etrv_temp_c:.2f} °C" if zone.raw_etrv_temp_c is not None else "—"
        rh_s = f"%{zone.rel_humidity_pct:.0f}" if zone.rel_humidity_pct is not None else "—"
        tau_s = f"{zone.tau_hours:.1f} sa" if zone.tau_hours is not None else "—"
        vent_s = (
            f"{zone.optimal_ventilation_min:.0f} dk"
            if zone.optimal_ventilation_min is not None
            else "—"
        )
        proj = proj_map.get(zone.zone_id)
        min_proj = (
            f"{proj['projected_min_temp_c']:.1f} °C"
            if isinstance(proj, Mapping) and proj.get("projected_min_temp_c") is not None
            else "—"
        )
        lines.append(
            f"- **{zone.name}** (`{zone.zone_id}`): Gerçek={t_true}, Ham eTRV={t_etrv}, "
            f"Nem={rh_s} ({zone.mold_risk}), τ={tau_s}, Gece Min Tahmin={min_proj}, "
            f"Önerilen Havalandırma={vent_s}"
        )

    hydraulic = history_summary.get("hydraulic_balance") or simulation.get("hydraulic_balance") or []
    if isinstance(hydraulic, list) and hydraulic:
        lines.extend(["", "### Hidrolik Denge"])
        for item in hydraulic[:5]:
            if not isinstance(item, Mapping):
                continue
            z_name = item.get("name") or item.get("zone_id")
            cls_label = item.get("classification", "insufficient_data")
            notes = item.get("notes", "")
            lines.append(f"- **{z_name}** (`{cls_label}`): {notes}")

    summary_state = (
        f"Uyarı: {len(snapshot.anomaly_zones)} anomali"
        if snapshot.anomaly_zones
        else f"Normal ({snapshot.monitored_zone_count} zon izleniyor)"
    )
    return summary_state, "\n".join(lines)


class ThermalTwinService:
    """Lifecycle-managed spatial thermal digital twin service."""

    def __init__(
        self,
        config: AppConfig,
        ha_adapter: HomeAssistantWSAdapter | None = None,
        entity_resolver: EntityResolver | None = None,
        history_client: HAHistoryClient | None = None,
        store: ThermalStore | None = None,
        advisor_runner: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._config = config
        self._thermal_cfg = config.thermal
        self._ha_adapter = ha_adapter
        self._entity_resolver = entity_resolver
        self._owns_history_client = history_client is None
        self._history_client = history_client or HAHistoryClient(
            config=config,
            ha_adapter=ha_adapter,
        )
        self._store = store or ThermalStore(config.sqlite_db_path)
        self._advisor_runner = advisor_runner
        self._adapter = NeonPlanAdapter(
            default_ceiling_height_m=self._thermal_cfg.default_ceiling_height_m,
            radiator_proximity_threshold_m=self._thermal_cfg.radiator_proximity_threshold_m,
            default_etrv_bias_factor=self._thermal_cfg.default_etrv_bias_factor,
        )
        self._topology: BuildingTopology = BuildingTopology()
        self._engine: ThermalPhysicsEngine | None = None
        self._latest_snapshot: ThermalSnapshotSummary | None = None
        self._initialized = False
        self._running = False
        self._stop_event = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._anomaly_cooldowns: dict[str, float] = {}
        self._last_briefing_date: date | None = None
        self._last_registry_marker: tuple[int, int, str | None] = (0, 0, None)
        self._latest_advisor_report: ThermalAdvisorReportRecord | None = None
        self._active_report_type: str = "manual"

    @property
    def topology(self) -> BuildingTopology:
        return self._topology

    @property
    def engine(self) -> ThermalPhysicsEngine | None:
        return self._engine

    @property
    def store(self) -> ThermalStore:
        return self._store

    @property
    def history_client(self) -> HAHistoryClient:
        return self._history_client

    @property
    def latest_snapshot(self) -> ThermalSnapshotSummary | None:
        return self._latest_snapshot

    @property
    def advisor_runner(self) -> Callable[[str], Awaitable[str]] | None:
        return self._advisor_runner

    def set_advisor_runner(self, runner: Callable[[str], Awaitable[str]] | None) -> None:
        """Attach or replace the async LLM advisor callback."""
        self._advisor_runner = runner

    def _load_topology_sync(
        self,
        registry_records: list[RegistryEntityRecord] | None,
        entity_aliases: Mapping[str, str] | None = None,
    ) -> BuildingTopology:
        plan_path = self._config.neonplan_json_path
        if not plan_path.exists():
            logger.warning(
                "thermal | neonplan_json_path '%s' not found; starting with empty BuildingTopology",
                plan_path,
            )
            return BuildingTopology()
        overrides_path = (
            self._config.thermal_overrides_path
            if self._config.thermal_overrides_path.exists()
            else None
        )
        return self._adapter.load(
            json_path=plan_path,
            overrides_path=overrides_path,
            registry_records=registry_records,
            entity_aliases=entity_aliases,
        )

    async def _load_topology(self) -> BuildingTopology:
        if self._entity_resolver is not None:
            records = self._entity_resolver.list_registry_records()
            aliases = self._entity_resolver.rename_map
            self._last_registry_marker = (
                len(records),
                self._entity_resolver.registry_revision,
                self._entity_resolver.last_sync_utc,
            )
        else:
            records = None
            aliases = None
            self._last_registry_marker = (0, 0, None)
        return await asyncio.to_thread(self._load_topology_sync, records, aliases)

    async def initialize(self) -> None:
        """Initialize SQLite store, parse building topology, and build physics engine."""
        await self._store.initialize()
        self._topology = await self._load_topology()
        self._engine = ThermalPhysicsEngine(
            topology=self._topology,
            store=self._store,
            settings=self._thermal_cfg,
            tz=self._config.tzinfo,
        )
        self._latest_advisor_report = await self._store.get_advisor_report("latest")
        if (
            self._latest_advisor_report is not None
            and self._latest_advisor_report.report_type == "daily_briefing"
        ):
            raw_ts = self._latest_advisor_report.updated_at.strip()
            if raw_ts.endswith("Z"):
                raw_ts = f"{raw_ts[:-1]}+00:00"
            try:
                dt = datetime.fromisoformat(raw_ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                self._last_briefing_date = dt.astimezone(self._config.tzinfo).date()
            except ValueError:
                pass
        self._initialized = True
        logger.info(
            "thermal | initialized with %d zone(s) (%d monitored)",
            len(self._topology.zones),
            len(self._topology.monitored_zones()),
        )

    async def _ensure_engine(self) -> ThermalPhysicsEngine:
        if not self._initialized or self._engine is None:
            await self.initialize()
        assert self._engine is not None
        return self._engine

    async def refresh_topology(self) -> BuildingTopology:
        """Re-parse NeonPlan floorplan with current HA registry records and update engine."""
        engine = await self._ensure_engine()
        self._topology = await self._load_topology()
        engine.update_topology(self._topology)
        return self._topology

    async def start(self) -> None:
        """Start the periodic thermal twin background loop."""
        await self._ensure_engine()
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._loop_task = asyncio.create_task(self._run_loop(), name="thermal_twin_loop")
        logger.info(
            "thermal | background loop started (interval=%ds, briefing=%s (%s))",
            self._thermal_cfg.tick_interval_sec,
            self._thermal_cfg.daily_briefing_time,
            self._config.timezone,
        )

    async def stop(self) -> None:
        """Stop the periodic thermal twin background loop cleanly."""
        was_running = self._running or self._loop_task is not None
        self._running = False
        self._stop_event.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        if was_running:
            logger.info("thermal | background loop stopped")

    async def aclose(self) -> None:
        """Stop background work and close owned HTTP resources."""
        await self.stop()
        if self._owns_history_client:
            await self._history_client.aclose()

    async def _handle_anomalies(
        self,
        snapshot: ThermalSnapshotSummary,
        *,
        publish: bool,
    ) -> None:
        if not snapshot.anomaly_zones:
            return
        now_mono = time.monotonic()
        fresh_zones = [
            zid
            for zid in snapshot.anomaly_zones
            if zid not in self._anomaly_cooldowns
            or (now_mono - self._anomaly_cooldowns[zid]) >= _ANOMALY_DEBOUNCE_SEC
        ]
        if not fresh_zones:
            return

        for zid in fresh_zones:
            self._anomaly_cooldowns[zid] = now_mono

        anomaly_details = [
            snapshot.zones[zid].model_dump(mode="json")
            for zid in fresh_zones
            if zid in snapshot.zones
        ]
        payload = {
            "event": "thermal_anomaly_detected",
            "timestamp_utc": snapshot.timestamp_utc,
            "anomaly_zones": fresh_zones,
            "zone_details": anomaly_details,
        }
        if self._advisor_runner is not None:
            prev_type = self._active_report_type
            self._active_report_type = "anomaly"
            try:
                await self._advisor_runner(json.dumps(payload, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                logger.warning("thermal | anomaly advisor runner failed: %s", exc)
            finally:
                self._active_report_type = prev_type
            return

        if self._thermal_cfg.publish_virtual_sensors and publish:
            reasons = [
                f"- **{z.name}** (`{z.zone_id}`): {z.anomaly_reason or 'anomaly'}"
                for z in (snapshot.zones[zid] for zid in fresh_zones if zid in snapshot.zones)
            ]
            summary = f"Termal Uyarı: {', '.join(fresh_zones)}"
            md = f"## Termal İkiz Anomali Uyarısı ({snapshot.timestamp_utc})\n\n" + "\n".join(reasons)
            await self.publish_advisor_report(
                summary_state=summary,
                report_markdown=md,
                report_type="anomaly",
            )

    async def _maybe_refresh_topology(self) -> None:
        if self._entity_resolver is not None and self._config.neonplan_json_path.exists():
            current_marker = (
                self._entity_resolver.registry_size,
                self._entity_resolver.registry_revision,
                self._entity_resolver.last_sync_utc,
            )
            if self._entity_resolver.registry_size > 0 and current_marker != self._last_registry_marker:
                await self.refresh_topology()

    async def run_live_tick(
        self,
        publish: bool = True,
        *,
        trigger_advisor: bool = True,
    ) -> ThermalSnapshotSummary:
        """Fetch live HA states, compute fused thermal snapshot, and optionally publish sensors."""
        engine = await self._ensure_engine()
        await self._maybe_refresh_topology()

        raw_states = await self._history_client.fetch_all_states()
        snapshot = await engine.update_live_states(raw_states)
        self._latest_snapshot = snapshot

        if self._thermal_cfg.publish_virtual_sensors and publish:
            await self._history_client.publish_snapshot_sensors(snapshot)
            await self._republish_advisor_if_missing(raw_states)

        if trigger_advisor:
            await self._handle_anomalies(snapshot, publish=publish)

        return snapshot

    async def _republish_advisor_if_missing(self, raw_states: Sequence[Mapping[str, Any]]) -> bool:
        """Re-publish cached advisor report if HA restarted and cleared virtual entities."""
        rec = self._latest_advisor_report
        if rec is None:
            rec = await self._store.get_advisor_report("latest")
            self._latest_advisor_report = rec
        if rec is None:
            return False

        target_eid = self._thermal_cfg.advisor_report_entity
        existing_state: str | None = None
        for item in raw_states:
            if isinstance(item, Mapping) and str(item.get("entity_id", "")).strip() == target_eid:
                raw_st = item.get("state")
                existing_state = str(raw_st).strip() if raw_st is not None else ""
                break

        if existing_state is not None and existing_state.lower() not in {"", "unknown", "unavailable", "none"}:
            return False

        attrs = dict(rec.extra_attributes)
        attrs["original_updated_at_utc"] = rec.updated_at
        attrs["republished_after_ha_restart"] = True
        await self._history_client.publish_advisor_report(
            summary_state=rec.summary_state,
            report_markdown=rec.report_markdown,
            extra_attributes=attrs,
        )
        logger.info(
            "thermal | republished missing '%s' to HA after restart (original_updated_at=%s)",
            target_eid,
            rec.updated_at,
        )
        return True

    def _gather_history_entity_ids(self, zone_id: str | None) -> tuple[list[str], str | None]:
        if zone_id is not None:
            resolved = self._topology.resolve_zone(zone_id)
            if resolved is None:
                return [], None
            target_zones = [resolved]
            resolved_zone_id: str | None = resolved.zone_id
        else:
            target_zones = self._topology.monitored_zones()
            resolved_zone_id = None

        candidates: list[str] = []
        for zone in target_zones:
            candidates.extend(zone.climate_entities)
            candidates.extend(sp.entity_id for sp in zone.temperature_sensors)
            candidates.extend(zone.humidity_sensors)
            candidates.extend(zone.valve_position_sensors)
            candidates.extend(zone.contact_sensors)

        if self._thermal_cfg.outdoor_temp_entity:
            candidates.append(self._thermal_cfg.outdoor_temp_entity)
        if self._thermal_cfg.outdoor_weather_entity:
            candidates.append(self._thermal_cfg.outdoor_weather_entity)
        if self._topology.weather_entity:
            candidates.append(self._topology.weather_entity)
        candidates.extend(self._topology.outdoor_temp_sensors)
        return _dedupe_str(candidates), resolved_zone_id

    async def run_history_analysis(
        self,
        zone_id: str | None = None,
        lookback_hours: int = 24,
    ) -> dict[str, Any]:
        """Fetch HA history for target zone(s) + outdoor sensors and run grey-box calibration."""
        engine = await self._ensure_engine()
        await self._maybe_refresh_topology()
        hours = max(1, min(_MAX_LOOKBACK_HOURS, int(lookback_hours)))
        end_utc = datetime.now(UTC)
        start_utc = end_utc - timedelta(hours=hours)

        if zone_id is not None:
            entity_ids, resolved_zid = self._gather_history_entity_ids(zone_id)
            if resolved_zid is None:
                return await engine.analyze_history({}, zone_id=zone_id, now_utc=end_utc)
        else:
            entity_ids, resolved_zid = self._gather_history_entity_ids(None)

        history: dict[str, list[dict[str, Any]]] = {}
        if entity_ids:
            history = await self._history_client.fetch_history(
                entity_ids=entity_ids,
                start_utc=start_utc,
                end_utc=end_utc,
            )

        return await engine.analyze_history(
            history_by_entity=history,
            zone_id=resolved_zid,
            now_utc=end_utc,
        )

    def _is_snapshot_fresh(self) -> bool:
        if self._latest_snapshot is None:
            return False
        raw_ts = self._latest_snapshot.timestamp_utc
        if raw_ts.endswith("Z"):
            raw_ts = f"{raw_ts[:-1]}+00:00"
        try:
            ts = datetime.fromisoformat(raw_ts)
        except ValueError:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age_sec = (datetime.now(UTC) - ts).total_seconds()
        return 0.0 <= age_sec <= float(self._thermal_cfg.tick_interval_sec)

    async def simulate(
        self,
        zone_id: str | None = None,
        outdoor_temp_c: float | None = None,
        outdoor_humidity_pct: float | None = None,
        hours_ahead: float = 8.0,
        target_setpoint_c: float | None = None,
    ) -> dict[str, Any]:
        """Ensure a fresh snapshot and run overnight cooling / warm-up / ventilation projections."""
        engine = await self._ensure_engine()
        if not self._is_snapshot_fresh():
            try:
                await self.run_live_tick(publish=False, trigger_advisor=False)
            except HomeAssistantError as exc:
                logger.warning("thermal | live refresh before simulation failed: %s", exc)

        return await engine.simulate_scenario(
            live_snapshot=self._latest_snapshot,
            zone_id=zone_id,
            outdoor_temp_c=outdoor_temp_c,
            outdoor_humidity_pct=outdoor_humidity_pct,
            hours_ahead=hours_ahead,
            target_setpoint_c=target_setpoint_c,
        )

    async def publish_advisor_report(
        self,
        summary_state: str,
        report_markdown: str,
        extra_attributes: Mapping[str, Any] | None = None,
        *,
        report_type: str | None = None,
    ) -> dict[str, Any]:
        """Publish the thermal advisor Markdown report to Home Assistant and persist it."""
        result = await self._history_client.publish_advisor_report(
            summary_state=summary_state,
            report_markdown=report_markdown,
            extra_attributes=extra_attributes,
        )
        eff_type = report_type or self._active_report_type
        self._latest_advisor_report = await self._store.save_advisor_report(
            ThermalAdvisorReportRecord(
                report_key="latest",
                summary_state=summary_state,
                report_markdown=report_markdown,
                extra_attributes=dict(extra_attributes or {}),
                report_type=eff_type,
            )
        )
        return result

    async def run_daily_briefing(self) -> str:
        """Run 24h calibration + live tick + 8h projection and publish advisor report."""
        history_summary = await self.run_history_analysis(zone_id=None, lookback_hours=24)
        snapshot = await self.run_live_tick(publish=True, trigger_advisor=False)
        simulation = await self.simulate(zone_id=None, hours_ahead=8.0)

        payload = {
            "event": "daily_thermal_briefing",
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "snapshot": snapshot.model_dump(mode="json"),
            "history_analysis_24h": history_summary,
            "overnight_simulation_8h": simulation,
        }
        prev_type = self._active_report_type
        self._active_report_type = "daily_briefing"
        try:
            if self._advisor_runner is not None:
                try:
                    return await self._advisor_runner(json.dumps(payload, ensure_ascii=False))
                except Exception as exc:
                    logger.warning(
                        "thermal_service | advisor_runner failed, falling back to deterministic briefing: %s",
                        exc,
                    )

            summary_state, report_md = _format_deterministic_briefing(
                snapshot=snapshot,
                history_summary=history_summary,
                simulation=simulation,
            )
            await self.publish_advisor_report(
                summary_state=summary_state,
                report_markdown=report_md,
                report_type="daily_briefing",
            )
            return report_md
        finally:
            self._active_report_type = prev_type

    async def _maybe_run_daily_briefing(self, now_local: datetime | None = None) -> bool:
        tz = self._config.tzinfo
        if now_local is None:
            current = datetime.now(tz)
        elif now_local.tzinfo is None:
            current = now_local.replace(tzinfo=tz)
        else:
            current = now_local.astimezone(tz)
        today = current.date()
        if self._last_briefing_date == today:
            return False
        target_h, target_m = _parse_hhmm(self._thermal_cfg.daily_briefing_time)
        if (current.hour, current.minute) < (target_h, target_m):
            return False
        self._last_briefing_date = today
        await self.run_daily_briefing()
        return True

    async def _run_loop(self) -> None:
        first_tick = True
        while self._running:
            wait_sec = 2.0 if first_tick else float(self._thermal_cfg.tick_interval_sec)
            first_tick = False
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=wait_sec,
                )
                break
            except TimeoutError:
                pass

            if not self._running:
                break

            try:
                await self.run_live_tick(publish=True, trigger_advisor=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("thermal | background live tick failed: %s", exc)

            try:
                await self._maybe_run_daily_briefing()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("thermal | scheduled daily briefing failed: %s", exc)


def build_thermal_tools(service: ThermalTwinService) -> list[AgentTool]:
    """Return Strands tools backed by ``service`` for the Thermal Digital Twin sub-agent."""

    @tool(
        name="get_thermal_twin_snapshot",
        description=(
            "Return fused live thermal digital twin telemetry across all zones (or one zone): "
            "true ambient temperature vs. raw radiator eTRV temperature, spatial gradient, valve %, "
            "dew point, absolute humidity, mold risk, window/frost protection, k_loss, and anomalies."
        ),
    )
    async def get_thermal_twin_snapshot(
        zone_id: str | None = None,
        refresh: bool = True,
    ) -> dict[str, Any]:
        """Get the live thermal digital twin snapshot for the whole building or a specific zone.

        Args:
            zone_id: Optional zone identifier or room name (e.g. 'living_room').
            refresh: Fetch fresh Home Assistant states before returning (default True).
        """
        if refresh or service.latest_snapshot is None:
            snapshot = await service.run_live_tick(publish=False, trigger_advisor=False)
        else:
            snapshot = service.latest_snapshot

        if zone_id is not None:
            resolved = service.topology.resolve_zone(zone_id)
            target_id = resolved.zone_id if resolved is not None else zone_id
            zone_tel = snapshot.zones.get(target_id)
            if zone_tel is None:
                return {
                    "error": f"Unknown or unmonitored zone: {zone_id}",
                    "available_zones": list(snapshot.zones.keys()),
                    "timestamp_utc": snapshot.timestamp_utc,
                }
            return {
                "timestamp_utc": snapshot.timestamp_utc,
                "outdoor_temp_c": snapshot.outdoor_temp_c,
                "outdoor_humidity_pct": snapshot.outdoor_humidity_pct,
                "outdoor_abs_humidity_g_m3": snapshot.outdoor_abs_humidity_g_m3,
                "outdoor_dew_point_c": snapshot.outdoor_dew_point_c,
                "zone": zone_tel.model_dump(mode="json"),
            }
        return snapshot.model_dump(mode="json")

    @tool(
        name="analyze_zone_history",
        description=(
            "Analyze historical Home Assistant time-series to calibrate eTRV bias factors, fit "
            "nighttime RC cooling constants (k_loss, tau_hours), detect window-open ventilation "
            "shocks (moisture knee point & rebound), evaluate hydraulic balance, and detect stalls."
        ),
    )
    async def analyze_zone_history(
        zone_id: str | None = None,
        lookback_hours: int = 24,
    ) -> dict[str, Any]:
        """Run historical thermal physics analysis and self-calibration.

        Args:
            zone_id: Optional zone identifier or room name; omit for all monitored zones.
            lookback_hours: Historical window in hours (1 to 168, default 24).
        """
        return await service.run_history_analysis(
            zone_id=zone_id,
            lookback_hours=lookback_hours,
        )

    @tool(
        name="simulate_thermal_scenario",
        description=(
            "Simulate future overnight cooling trajectories, estimated morning warm-up minutes, "
            "optimal shock-ventilation duration, and cross-zone hydraulic valve balancing."
        ),
    )
    async def simulate_thermal_scenario(
        zone_id: str | None = None,
        outdoor_temp_c: float | None = None,
        outdoor_humidity_pct: float | None = None,
        hours_ahead: float = 8.0,
        target_setpoint_c: float | None = None,
    ) -> dict[str, Any]:
        """Project future indoor temperatures and heating/ventilation metrics.

        Args:
            zone_id: Optional zone identifier or room name; omit for all monitored zones.
            outdoor_temp_c: Optional assumed outdoor temperature (°C); defaults to live outdoor temp.
            outdoor_humidity_pct: Optional assumed outdoor relative humidity (%).
            hours_ahead: Projection horizon in hours (default 8.0).
            target_setpoint_c: Optional target indoor temperature (°C) for warm-up estimation.
        """
        return await service.simulate(
            zone_id=zone_id,
            outdoor_temp_c=outdoor_temp_c,
            outdoor_humidity_pct=outdoor_humidity_pct,
            hours_ahead=hours_ahead,
            target_setpoint_c=target_setpoint_c,
        )

    @tool(
        name="publish_thermal_advisor_report",
        description=(
            "Publish a concise status summary and rich Markdown thermal advisor report to the "
            "Home Assistant dashboard sensor (`sensor.thermal_twin_advisor_report`)."
        ),
    )
    async def publish_thermal_advisor_report(
        summary_state: str,
        report_markdown: str,
    ) -> dict[str, Any]:
        """Publish the thermal advisor report sensor to Home Assistant.

        Args:
            summary_state: Short status string for the sensor state (<= 240 chars).
            report_markdown: Full Markdown report stored in the `report_markdown` attribute.
        """
        return await service.publish_advisor_report(
            summary_state=summary_state,
            report_markdown=report_markdown,
        )

    return [
        get_thermal_twin_snapshot,
        analyze_zone_history,
        simulate_thermal_scenario,
        publish_thermal_advisor_report,
    ]
