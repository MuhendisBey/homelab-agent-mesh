"""Deterministic grey-box RC physics engine for the spatial thermal digital twin."""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import warnings
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, Field

from src.core.config import ThermalSettings
from src.core.neonplan_adapter import BuildingTopology, SensorPlacement, ZoneTopology
from src.core.psychrometrics import (
    calculate_absolute_humidity,
    calculate_dew_point,
    calculate_enthalpy,
    sanitize_humidity,
)
from src.memory.thermal_store import (
    ThermalObservationRecord,
    ThermalStore,
    ThermalZoneModelRecord,
)

logger = logging.getLogger(__name__)

_INVALID_STATES: frozenset[str] = frozenset({"", "unavailable", "unknown", "none", "nan", "null"})
_OPEN_STATES: frozenset[str] = frozenset({"on", "open"})
_AC_CLIMATE_HINTS: tuple[str, ...] = ("climatecontrol", "klima", "air_conditioner", "ac_unit")


# ---------------------------------------------------------------------------
# Output Pydantic models
# ---------------------------------------------------------------------------


class ZoneLiveTelemetry(BaseModel):
    """Live fused temperature, psychrometric health, and projection telemetry for one zone."""

    zone_id: str
    name: str
    floor_name: str
    volume_m3: float
    timestamp_utc: str | None = None
    climate_entities: list[str] = Field(default_factory=list)
    target_setpoint_c: float | None = None
    hvac_action: str | None = None
    bosch_state: str | None = None
    valve_position_pct: float | None = None
    max_valve_pct: float | None = None
    valve_estimated: bool = False
    raw_etrv_temp_c: float | None = None
    etrv_corrected_temp_c: float | None = None
    ambient_temp_c: float | None = None
    true_temp_c: float | None = None
    true_ambient_temp_c: float | None = None
    spatial_gradient_c: float | None = None
    rel_humidity_pct: float | None = None
    humidity_source: str | None = None
    absolute_humidity_g_m3: float | None = None
    dew_point_c: float | None = None
    enthalpy_kj_kg: float | None = None
    mold_risk: Literal["low", "moderate", "high", "unknown"] = "unknown"
    window_open: bool = False
    frost_protection_active: bool = False
    k_loss: float = 0.08
    tau_hours: float | None = None
    cooling_rate_c_per_h: float | None = None
    warmup_rate_c_per_h: float | None = None
    etrv_bias_factor: float = 2.5
    static_offset_c: float = 0.0
    night_min_temp_c: float | None = None
    optimal_ventilation_min: float | None = None
    anomaly_detected: bool = False
    anomaly_reason: str | None = None


class NightCoolingAnalysis(BaseModel):
    """Fitted Newton cooling curve and envelope loss constant for one night run."""

    zone_id: str
    name: str = ""
    started_at: str
    ended_at: str
    duration_min: float
    sample_count: int
    t_start_c: float
    t_min_c: float
    t_end_c: float
    outdoor_temp_mean_c: float | None = None
    k_loss: float
    tau_hours: float
    cooling_rate_c_per_h: float
    r_squared: float
    fit_method: Literal["scipy_curve_fit", "log_linear_fallback", "linear_fallback"] = "scipy_curve_fit"


class VentilationShockEvent(BaseModel):
    """Detected window-open shock ventilation episode with moisture knee point and rebound."""

    zone_id: str
    name: str = ""
    contact_entity: str | None = None
    started_at: str
    ended_at: str
    duration_min: float
    indoor_temp_start_c: float
    indoor_temp_end_c: float
    indoor_temp_drop_c: float
    outdoor_temp_c: float | None = None
    outdoor_humidity_pct: float | None = None
    indoor_humidity_start_pct: float | None = None
    indoor_humidity_end_pct: float | None = None
    indoor_abs_humidity_start_g_m3: float | None = None
    indoor_abs_humidity_end_g_m3: float | None = None
    indoor_abs_humidity_drop: float | None = None
    enthalpy_loss_kj: float | None = None
    rebound_temp_c: float | None = None
    rebound_delta_c: float | None = None
    knee_point_min: float | None = None


class HydraulicZoneComparison(BaseModel):
    """Cross-zone warm-up comparison and max-valve balancing recommendation."""

    zone_id: str
    name: str
    volume_m3: float
    avg_warmup_rate_c_per_h: float | None = None
    avg_cooling_rate_c_per_h: float | None = None
    k_loss: float
    relative_speed_ratio: float | None = None
    classification: Literal[
        "fast_heating",
        "balanced",
        "slow_heating",
        "stalled",
        "insufficient_data",
    ] = "insufficient_data"
    max_valve_entity: str | None = None
    recommended_max_valve_pct: int | None = None
    anomaly_detected: bool = False
    notes: str = ""


class ScenarioProjection(BaseModel):
    """Overnight cooling, warm-up time, and ventilation projection for one zone."""

    zone_id: str
    name: str
    current_temp_c: float | None = None
    k_loss: float
    hours_ahead: float
    hourly_temps_c: list[float] = Field(default_factory=list)
    projected_min_temp_c: float | None = None
    target_setpoint_c: float | None = None
    estimated_warmup_min: float | None = None
    recommended_ventilation_min: float | None = None


class ThermalSnapshotSummary(BaseModel):
    """Whole-building live thermal & psychrometric diagnostic snapshot."""

    timestamp_utc: str
    outdoor_temp_c: float | None = None
    outdoor_humidity_pct: float | None = None
    outdoor_abs_humidity_g_m3: float | None = None
    outdoor_dew_point_c: float | None = None
    outdoor_enthalpy_kj_kg: float | None = None
    building_ref_humidity_pct: float | None = None
    monitored_zone_count: int = 0
    open_window_zones: list[str] = Field(default_factory=list)
    high_mold_risk_zones: list[str] = Field(default_factory=list)
    anomaly_zones: list[str] = Field(default_factory=list)
    zones: dict[str, ZoneLiveTelemetry] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal parsing & time-series helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _HistoryPoint:
    ts: datetime
    state_str: str
    numeric_val: float | None
    attrs: dict[str, Any]


def _parse_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in _INVALID_STATES:
            return None
        try:
            num = float(cleaned)
        except ValueError:
            return None
        return num if math.isfinite(num) else None
    if isinstance(value, (int, float)):
        num = float(value)
        return num if math.isfinite(num) else None
    return None


def _parse_timestamp(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if not math.isfinite(float(raw)):
            return None
        return datetime.fromtimestamp(float(raw), tz=UTC)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return None


def _extract_history_points(records: Sequence[Mapping[str, Any]] | None) -> list[_HistoryPoint]:
    if not records:
        return []
    parsed: list[_HistoryPoint] = []
    for rec in records:
        ts = _parse_timestamp(
            rec.get("last_updated")
            or rec.get("last_changed")
            or rec.get("timestamp")
            or rec.get("lu")
            or rec.get("lc")
        )
        if ts is None:
            continue
        raw_state = rec.get("state") if "state" in rec else rec.get("s")
        state_str = str(raw_state).strip() if raw_state is not None else ""
        raw_attrs = rec.get("attributes") if "attributes" in rec else rec.get("a")
        attrs = dict(raw_attrs) if isinstance(raw_attrs, Mapping) else {}
        parsed.append(
            _HistoryPoint(
                ts=ts,
                state_str=state_str,
                numeric_val=_parse_float(raw_state),
                attrs=attrs,
            )
        )
    parsed.sort(key=lambda pt: pt.ts)
    deduped: list[_HistoryPoint] = []
    for pt in parsed:
        if deduped and deduped[-1].ts == pt.ts:
            deduped[-1] = pt
        else:
            deduped.append(pt)
    return deduped


def _is_ac_climate_id(entity_id: str) -> bool:
    low = entity_id.lower()
    if any(hint in low for hint in _AC_CLIMATE_HINTS):
        return True
    obj = low.split(".", 1)[-1]
    return "ac" in obj.split("_")


def _extract_live_temp(sp: SensorPlacement, state_map: Mapping[str, Mapping[str, Any]]) -> float | None:
    st = state_map.get(sp.entity_id)
    if st is None:
        return None
    attrs = st.get("attributes")
    attr_map: Mapping[str, Any] = attrs if isinstance(attrs, Mapping) else {}
    if sp.entity_id.startswith("climate."):
        return _parse_float(attr_map.get("current_temperature"))
    val = _parse_float(st.get("state"))
    if val is not None:
        return val
    return _parse_float(attr_map.get("temperature") or attr_map.get("current_temperature"))


def _compute_optimal_ventilation_min(
    volume_m3: float,
    window_area_m2: float,
    indoor_temp_c: float | None,
    outdoor_temp_c: float | None,
    indoor_abs_humidity_g_m3: float | None,
    outdoor_abs_humidity_g_m3: float | None,
    indoor_rh_pct: float | None,
) -> float | None:
    if indoor_temp_c is None:
        return None
    eff_vol = max(volume_m3, 10.0)
    eff_area = max(window_area_m2, 0.8)
    delta_t = max(abs(indoor_temp_c - outdoor_temp_c), 2.0) if outdoor_temp_c is not None else 10.0
    ach = 45.0 * (eff_area / eff_vol) * math.sqrt(delta_t)
    base_min = 60.0 / max(ach, 1.5)

    if indoor_rh_pct is not None and indoor_rh_pct > 55.0:
        base_min *= 1.0 + 0.30 * min(1.0, (indoor_rh_pct - 55.0) / 25.0)

    if (
        indoor_abs_humidity_g_m3 is not None
        and outdoor_abs_humidity_g_m3 is not None
        and outdoor_abs_humidity_g_m3 >= indoor_abs_humidity_g_m3
    ):
        base_min = min(base_min, 4.0)

    return round(min(25.0, max(3.0, base_min)), 1)


def _interpolate_series(
    series: Sequence[tuple[datetime, float]],
    target: datetime,
    max_gap_sec: float = 10800.0,
    edge_tol_sec: float = 3600.0,
) -> float | None:
    if not series:
        return None
    times = [pt[0] for pt in series]
    idx = bisect_right(times, target)
    if idx == 0:
        if (times[0] - target).total_seconds() <= edge_tol_sec:
            return series[0][1]
        return None
    if idx == len(series):
        if (target - times[-1]).total_seconds() <= edge_tol_sec:
            return series[-1][1]
        return None
    t0, v0 = series[idx - 1]
    t1, v1 = series[idx]
    dt = (t1 - t0).total_seconds()
    if dt <= 1e-6:
        return v1
    if dt <= max_gap_sec:
        frac = (target - t0).total_seconds() / dt
        return v0 + frac * (v1 - v0)
    d_left = (target - t0).total_seconds()
    d_right = (t1 - target).total_seconds()
    if min(d_left, d_right) <= edge_tol_sec:
        return v0 if d_left <= d_right else v1
    return None


def _lookup_step(points: Sequence[_HistoryPoint], target: datetime) -> _HistoryPoint | None:
    if not points:
        return None
    times = [pt.ts for pt in points]
    idx = bisect_right(times, target)
    if idx == 0:
        return None
    return points[idx - 1]


def _in_night_window(dt: datetime, start_hour: int, end_hour: int, tz: tzinfo = UTC) -> bool:
    local_dt = dt.astimezone(tz) if dt.tzinfo is not None else dt.replace(tzinfo=tz)
    hour = local_dt.hour
    minute = local_dt.minute
    if start_hour <= end_hour:
        return (start_hour <= hour < end_hour) or (hour == end_hour and minute == 0)
    return (hour >= start_hour) or (hour < end_hour) or (hour == end_hour and minute == 0)


def _fit_newton_cooling(
    t_hours: NDArray[np.float64],
    y_temps: NDArray[np.float64],
    t_out: float,
) -> tuple[float, float, Literal["scipy_curve_fit", "log_linear_fallback", "linear_fallback"]]:
    """Fit ``T_in(t) = T_out + (T_0 - T_out) * exp(-k_loss * t)`` with safe fallbacks."""
    duration_h = max(float(t_hours[-1] - t_hours[0]), 1e-6)
    y_span = float(np.ptp(y_temps))
    y_mean = float(np.mean(y_temps))
    ss_tot = float(np.sum((y_temps - y_mean) ** 2))

    if y_temps[0] > t_out + 0.2 and y_span >= 0.02 and len(t_hours) >= 3:
        try:
            from scipy.optimize import curve_fit

            def _model(t_arr: NDArray[np.float64], k: float, t0: float) -> NDArray[np.float64]:
                res: NDArray[np.float64] = t_out + (t0 - t_out) * np.exp(-k * t_arr)
                return res

            p0 = [0.08, float(y_temps[0])]
            bounds = ([0.001, float(y_temps[0]) - 5.0], [2.0, float(y_temps[0]) + 5.0])
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                popt, _ = curve_fit(_model, t_hours, y_temps, p0=p0, bounds=bounds, maxfev=4000)
            k_fit = float(popt[0])
            t0_fit = float(popt[1])
            preds = _model(t_hours, k_fit, t0_fit)
            ss_res = float(np.sum((y_temps - preds) ** 2))
            r2 = (1.0 - ss_res / ss_tot) if ss_tot > 1e-9 else 1.0
            if math.isfinite(k_fit) and 0.001 <= k_fit <= 2.0:
                return k_fit, max(0.0, min(1.0, r2)), "scipy_curve_fit"
        except (RuntimeError, ValueError, TypeError) as exc:
            logger.debug("curve_fit cooling regression fell back: %s", exc)

        # Log-linear fallback
        valid_mask = y_temps > (t_out + 0.1)
        if int(np.sum(valid_mask)) >= 2:
            t_v = t_hours[valid_mask]
            diff_v = y_temps[valid_mask] - t_out
            if diff_v[0] > 0.1 and float(np.ptp(t_v)) > 0.1:
                log_ratio = np.log(diff_v / diff_v[0])
                slope, _ = np.polyfit(t_v - t_v[0], log_ratio, 1)
                k_log = float(np.clip(-slope, 0.001, 2.0))
                preds = t_out + (float(y_temps[0]) - t_out) * np.exp(-k_log * t_hours)
                ss_res = float(np.sum((y_temps - preds) ** 2))
                r2 = (1.0 - ss_res / ss_tot) if ss_tot > 1e-9 else 1.0
                return k_log, max(0.0, min(1.0, r2)), "log_linear_fallback"

    cooling_rate = max(0.0, float(y_temps[0] - y_temps[-1]) / duration_h)
    k_lin = float(np.clip(cooling_rate / max(abs(y_mean - t_out), 1.0), 0.001, 2.0))
    preds = t_out + (float(y_temps[0]) - t_out) * np.exp(-k_lin * t_hours)
    ss_res = float(np.sum((y_temps - preds) ** 2))
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 1e-9 else (1.0 if ss_res <= 1e-9 else 0.0)
    return k_lin, max(0.0, min(1.0, r2)), "linear_fallback"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ThermalPhysicsEngine:
    """Deterministic grey-box RC physics & psychrometric engine for all building zones."""

    def __init__(
        self,
        topology: BuildingTopology,
        store: ThermalStore,
        settings: ThermalSettings,
        tz: tzinfo = UTC,
    ) -> None:
        self._topology = topology
        self._store = store
        self._settings = settings
        self._tz = tz

    @property
    def topology(self) -> BuildingTopology:
        """Return active building topology."""
        return self._topology

    def update_topology(self, topology: BuildingTopology) -> None:
        """Replace the active :class:`BuildingTopology` (e.g., after registry sync)."""
        self._topology = topology

    def _resolve_outdoor_live(
        self,
        state_map: Mapping[str, Mapping[str, Any]],
    ) -> tuple[float | None, float | None]:
        temp_c: float | None = None
        humidity: float | None = None

        temp_candidates: list[str] = []
        if self._settings.outdoor_temp_entity:
            temp_candidates.append(self._settings.outdoor_temp_entity)
        temp_candidates.extend(self._topology.outdoor_temp_sensors)
        if self._settings.outdoor_weather_entity:
            temp_candidates.append(self._settings.outdoor_weather_entity)
        if self._topology.weather_entity:
            temp_candidates.append(self._topology.weather_entity)

        for eid in temp_candidates:
            st = state_map.get(eid)
            if st is None:
                continue
            attrs = st.get("attributes")
            attr_map: Mapping[str, Any] = attrs if isinstance(attrs, Mapping) else {}
            if eid.startswith("weather."):
                cand_t = _parse_float(attr_map.get("temperature"))
            else:
                cand_t = _parse_float(st.get("state"))
                if cand_t is None:
                    cand_t = _parse_float(attr_map.get("temperature"))
            if cand_t is not None:
                temp_c = cand_t
                break

        weather_candidates: list[str] = []
        if self._settings.outdoor_weather_entity:
            weather_candidates.append(self._settings.outdoor_weather_entity)
        if self._topology.weather_entity:
            weather_candidates.append(self._topology.weather_entity)
        weather_candidates.extend(temp_candidates)

        for eid in weather_candidates:
            st = state_map.get(eid)
            if st is None:
                continue
            attrs = st.get("attributes")
            attr_map = attrs if isinstance(attrs, Mapping) else {}
            cand_h = sanitize_humidity(_parse_float(attr_map.get("humidity")))
            if cand_h is None and eid.startswith("sensor.") and "humidity" in eid.lower():
                cand_h = sanitize_humidity(_parse_float(st.get("state")))
            if cand_h is not None:
                humidity = cand_h
                break

        return temp_c, humidity

    @staticmethod
    def _extract_zone_humidity(
        zone: ZoneTopology,
        state_map: Mapping[str, Mapping[str, Any]],
    ) -> tuple[float | None, str | None]:
        for eid in zone.humidity_sensors:
            st = state_map.get(eid)
            if st is None:
                continue
            rh = sanitize_humidity(_parse_float(st.get("state")))
            if rh is None:
                attrs = st.get("attributes")
                if isinstance(attrs, Mapping):
                    rh = sanitize_humidity(_parse_float(attrs.get("humidity")))
            if rh is not None:
                return rh, eid
        for cid in zone.climate_entities:
            st = state_map.get(cid)
            if st is None:
                continue
            attrs = st.get("attributes")
            if isinstance(attrs, Mapping):
                rh = sanitize_humidity(_parse_float(attrs.get("current_humidity")))
                if rh is not None:
                    return rh, cid
        return None, None

    @staticmethod
    def _extract_zone_climate_fields(
        zone: ZoneTopology,
        state_map: Mapping[str, Mapping[str, Any]],
    ) -> tuple[float | None, str | None, str | None, float | None, float | None]:
        ordered_climates = [c for c in zone.climate_entities if not _is_ac_climate_id(c)] + [
            c for c in zone.climate_entities if _is_ac_climate_id(c)
        ]
        targets: list[float] = []
        actions: list[str] = []
        bosch_states: list[str] = []
        climate_temps: list[float] = []
        climate_valves: list[float] = []

        for cid in ordered_climates:
            st = state_map.get(cid)
            if st is None:
                continue
            attrs = st.get("attributes")
            attr_map: Mapping[str, Any] = attrs if isinstance(attrs, Mapping) else {}
            tgt = _parse_float(attr_map.get("temperature"))
            if tgt is not None:
                targets.append(tgt)
            cur = _parse_float(attr_map.get("current_temperature"))
            if cur is not None:
                climate_temps.append(cur)
            v_attr = _parse_float(attr_map.get("valve_position"))
            if v_attr is not None and 0.0 <= v_attr <= 100.0:
                climate_valves.append(v_attr)
            raw_act = attr_map.get("hvac_action")
            if isinstance(raw_act, str) and raw_act.strip():
                actions.append(raw_act.strip().lower())
            elif str(st.get("state", "")).strip().lower() == "off":
                actions.append("off")
            raw_bosch = attr_map.get("bosch_state")
            if isinstance(raw_bosch, str) and raw_bosch.strip():
                bosch_states.append(raw_bosch.strip())

        target_c = round(sum(targets) / len(targets), 2) if targets else None
        hvac_action: str | None = None
        if "heating" in actions:
            hvac_action = "heating"
        elif actions:
            hvac_action = actions[0]

        bosch_state: str | None = None
        for b in bosch_states:
            if b.lower() == "zone error":
                bosch_state = b
                break
        if bosch_state is None and bosch_states:
            bosch_state = bosch_states[0]

        cur_temp = (sum(climate_temps) / len(climate_temps)) if climate_temps else None
        c_valve = (sum(climate_valves) / len(climate_valves)) if climate_valves else None
        return target_c, hvac_action, bosch_state, cur_temp, c_valve

    async def update_live_states(
        self,
        raw_states: Sequence[Mapping[str, Any]],
        now_utc: datetime | None = None,
    ) -> ThermalSnapshotSummary:
        """Fuse live HA states with calibrated models into a building thermal snapshot."""
        now = now_utc or datetime.now(UTC)
        state_map: dict[str, Mapping[str, Any]] = {
            str(s["entity_id"]): s for s in raw_states if isinstance(s, Mapping) and s.get("entity_id")
        }
        models = await self._store.get_all_zone_models()

        outdoor_temp_c, outdoor_humidity_pct = self._resolve_outdoor_live(state_map)
        outdoor_ah: float | None = None
        outdoor_dp: float | None = None
        outdoor_enth: float | None = None
        if outdoor_temp_c is not None and outdoor_humidity_pct is not None:
            outdoor_ah = round(calculate_absolute_humidity(outdoor_temp_c, outdoor_humidity_pct), 3)
            outdoor_dp = round(calculate_dew_point(outdoor_temp_c, outdoor_humidity_pct), 2)
            outdoor_enth = round(calculate_enthalpy(outdoor_temp_c, outdoor_humidity_pct), 2)

        monitored = self._topology.monitored_zones()

        # Pass 1: collect valid indoor humidity readings across zones for building reference fallback
        zone_humidities: dict[str, tuple[float | None, str | None]] = {}
        valid_indoor_rhs: list[float] = []
        for zone in monitored:
            rh, rh_src = self._extract_zone_humidity(zone, state_map)
            zone_humidities[zone.zone_id] = (rh, rh_src)
            if rh is not None:
                valid_indoor_rhs.append(rh)

        building_ref_rh: float | None = (
            round(float(statistics.median(valid_indoor_rhs)), 2) if valid_indoor_rhs else None
        )

        # Pass 2: compute per-zone telemetry
        zone_results: dict[str, ZoneLiveTelemetry] = {}
        open_window_zones: list[str] = []
        high_mold_risk_zones: list[str] = []
        anomaly_zones: list[str] = []

        for zone in monitored:
            model = models.get(zone.zone_id)
            k_loss = model.k_loss if model is not None else 0.08
            bias_factor = (
                model.etrv_bias_factor if model is not None else self._settings.default_etrv_bias_factor
            )
            static_offset = model.static_offset_c if model is not None else 0.0

            target_c, hvac_action, bosch_state, climate_cur_temp, climate_valve = (
                self._extract_zone_climate_fields(zone, state_map)
            )

            # Collect temperature readings separated by placement
            etrv_readings: list[float] = []
            ambient_readings: list[tuple[float, float]] = []
            unknown_readings: list[float] = []

            for sp in zone.temperature_sensors:
                t_val = _extract_live_temp(sp, state_map)
                if t_val is None:
                    continue
                if sp.placement == "on_radiator":
                    etrv_readings.append(t_val)
                elif sp.placement == "ambient_room":
                    ambient_readings.append((t_val, max(sp.reliability_weight, 0.05)))
                else:
                    unknown_readings.append(t_val)

            raw_etrv_temp_c = (
                round(sum(etrv_readings) / len(etrv_readings), 3) if etrv_readings else None
            )
            ambient_temp_c: float | None = None
            ambient_conf = 0.0
            if ambient_readings:
                w_sum = sum(w for _, w in ambient_readings)
                ambient_temp_c = round(sum(v * w for v, w in ambient_readings) / w_sum, 3)
                ambient_conf = max(w for _, w in ambient_readings)
            elif not etrv_readings and not unknown_readings and climate_cur_temp is not None:
                ambient_temp_c = round(climate_cur_temp, 3)
                ambient_conf = 0.3

            # Valve position extraction or estimation
            valve_vals: list[float] = []
            for veid in zone.valve_position_sensors:
                vst = state_map.get(veid)
                if vst is None:
                    continue
                vnum = _parse_float(vst.get("state"))
                if vnum is None:
                    v_attrs = vst.get("attributes")
                    if isinstance(v_attrs, Mapping):
                        vnum = _parse_float(v_attrs.get("valve_position"))
                if vnum is not None and 0.0 <= vnum <= 100.0:
                    valve_vals.append(vnum)

            valve_estimated = False
            valve_pct: float | None = None
            if valve_vals:
                valve_pct = round(sum(valve_vals) / len(valve_vals), 2)
            elif climate_valve is not None:
                valve_pct = round(climate_valve, 2)
            elif hvac_action == "heating":
                valve_estimated = True
                ref_t = ambient_temp_c if ambient_temp_c is not None else raw_etrv_temp_c
                if target_c is not None and ref_t is not None:
                    valve_pct = round(min(100.0, max(20.0, (target_c - ref_t) * 40.0)), 2)
                else:
                    valve_pct = 50.0
            elif hvac_action in ("idle", "off", "cooling"):
                valve_estimated = True
                valve_pct = 0.0

            max_valve_pct: float | None = None
            if zone.max_valve_entity:
                mv_state = state_map.get(zone.max_valve_entity)
                if mv_state is not None:
                    mv_num = _parse_float(mv_state.get("state"))
                    if mv_num is not None and 0.0 <= mv_num <= 100.0:
                        max_valve_pct = round(mv_num, 1)

            etrv_corrected_temp_c: float | None = None
            if raw_etrv_temp_c is not None:
                eff_valve = valve_pct if valve_pct is not None else 0.0
                etrv_corrected_temp_c = round(
                    raw_etrv_temp_c - static_offset - (bias_factor * (eff_valve / 100.0)),
                    3,
                )

            true_temp_c: float | None = None
            if ambient_temp_c is not None and etrv_corrected_temp_c is not None:
                w_amb = 0.80 * ambient_conf
                w_etrv = 0.20
                true_temp_c = round(
                    (w_amb * ambient_temp_c + w_etrv * etrv_corrected_temp_c) / (w_amb + w_etrv),
                    3,
                )
            elif ambient_temp_c is not None:
                true_temp_c = ambient_temp_c
            elif etrv_corrected_temp_c is not None:
                true_temp_c = etrv_corrected_temp_c
            elif unknown_readings:
                true_temp_c = round(sum(unknown_readings) / len(unknown_readings), 3)

            spatial_gradient_c = (
                round(raw_etrv_temp_c - ambient_temp_c, 3)
                if (raw_etrv_temp_c is not None and ambient_temp_c is not None)
                else None
            )

            rh_val, rh_src = zone_humidities[zone.zone_id]
            if rh_val is None and building_ref_rh is not None:
                rh_val = building_ref_rh
                rh_src = "building_fallback"

            abs_hum: float | None = None
            dew_point_c: float | None = None
            enthalpy_kj_kg: float | None = None
            mold_risk: Literal["low", "moderate", "high", "unknown"] = "unknown"
            if rh_val is not None and true_temp_c is not None:
                abs_hum = round(calculate_absolute_humidity(true_temp_c, rh_val), 3)
                dew_point_c = round(calculate_dew_point(true_temp_c, rh_val), 2)
                enthalpy_kj_kg = round(calculate_enthalpy(true_temp_c, rh_val), 2)
                if rh_val >= 70.0 or (true_temp_c - dew_point_c) <= 2.0:
                    mold_risk = "high"
                elif rh_val >= 62.0:
                    mold_risk = "moderate"
                else:
                    mold_risk = "low"
            elif rh_val is not None:
                mold_risk = "high" if rh_val >= 70.0 else ("moderate" if rh_val >= 62.0 else "low")

            window_open = any(
                str((state_map.get(ceid) or {}).get("state", "")).strip().lower() in _OPEN_STATES
                for ceid in zone.contact_sensors
            )
            frost_protection_active = window_open or (target_c is not None and target_c <= 8.0)

            night_min_temp_c: float | None = None
            if true_temp_c is not None and outdoor_temp_c is not None:
                night_min_temp_c = round(
                    outdoor_temp_c + (true_temp_c - outdoor_temp_c) * math.exp(-k_loss * 8.0),
                    2,
                )

            optimal_vent_min = _compute_optimal_ventilation_min(
                volume_m3=zone.volume_m3,
                window_area_m2=zone.window_area_m2,
                indoor_temp_c=true_temp_c,
                outdoor_temp_c=outdoor_temp_c,
                indoor_abs_humidity_g_m3=abs_hum,
                outdoor_abs_humidity_g_m3=outdoor_ah,
                indoor_rh_pct=rh_val,
            )

            anomaly_detected = False
            anomaly_reason: str | None = None
            if not frost_protection_active:
                if bosch_state is not None and bosch_state.strip().lower() == "zone error":
                    anomaly_detected = True
                    anomaly_reason = "bosch_zone_error"
                elif (
                    valve_pct is not None
                    and valve_pct >= self._settings.anomaly_valve_open_pct
                    and hvac_action == "off"
                ):
                    anomaly_detected = True
                    anomaly_reason = "valve_open_while_hvac_off"

            if window_open:
                open_window_zones.append(zone.zone_id)
            if mold_risk == "high":
                high_mold_risk_zones.append(zone.zone_id)
            if anomaly_detected:
                anomaly_zones.append(zone.zone_id)

            zone_results[zone.zone_id] = ZoneLiveTelemetry(
                zone_id=zone.zone_id,
                name=zone.name,
                floor_name=zone.floor_name,
                volume_m3=zone.volume_m3,
                timestamp_utc=now.astimezone(UTC).isoformat(),
                climate_entities=list(zone.climate_entities),
                target_setpoint_c=target_c,
                hvac_action=hvac_action,
                bosch_state=bosch_state,
                valve_position_pct=valve_pct,
                max_valve_pct=max_valve_pct,
                valve_estimated=valve_estimated,
                raw_etrv_temp_c=raw_etrv_temp_c,
                etrv_corrected_temp_c=etrv_corrected_temp_c,
                ambient_temp_c=ambient_temp_c,
                true_temp_c=true_temp_c,
                true_ambient_temp_c=true_temp_c,
                spatial_gradient_c=spatial_gradient_c,
                rel_humidity_pct=rh_val,
                humidity_source=rh_src,
                absolute_humidity_g_m3=abs_hum,
                dew_point_c=dew_point_c,
                enthalpy_kj_kg=enthalpy_kj_kg,
                mold_risk=mold_risk,
                window_open=window_open,
                frost_protection_active=frost_protection_active,
                k_loss=round(k_loss, 5),
                tau_hours=round(1.0 / max(k_loss, 1e-4), 2),
                cooling_rate_c_per_h=(model.avg_cooling_rate_c_per_h if model is not None else None),
                warmup_rate_c_per_h=(model.avg_warmup_rate_c_per_h if model is not None else None),
                etrv_bias_factor=round(bias_factor, 4),
                static_offset_c=round(static_offset, 4),
                night_min_temp_c=night_min_temp_c,
                optimal_ventilation_min=optimal_vent_min,
                anomaly_detected=anomaly_detected,
                anomaly_reason=anomaly_reason,
            )

        return ThermalSnapshotSummary(
            timestamp_utc=now.astimezone(UTC).isoformat(),
            outdoor_temp_c=outdoor_temp_c,
            outdoor_humidity_pct=outdoor_humidity_pct,
            outdoor_abs_humidity_g_m3=outdoor_ah,
            outdoor_dew_point_c=outdoor_dp,
            outdoor_enthalpy_kj_kg=outdoor_enth,
            building_ref_humidity_pct=building_ref_rh,
            monitored_zone_count=len(zone_results),
            open_window_zones=open_window_zones,
            high_mold_risk_zones=high_mold_risk_zones,
            anomaly_zones=anomaly_zones,
            zones=zone_results,
        )

    async def analyze_history(
        self,
        history_by_entity: Mapping[str, Sequence[Mapping[str, Any]]],
        zone_id: str | None = None,
        now_utc: datetime | None = None,
    ) -> dict[str, Any]:
        """Run self-calibration, RC night cooling, ventilation shock, and hydraulic analysis."""
        now = now_utc or datetime.now(UTC)
        if zone_id is not None:
            resolved = self._topology.resolve_zone(zone_id)
            if resolved is None:
                return {
                    "analyzed_at": now.astimezone(UTC).isoformat(),
                    "error": f"Unknown zone: {zone_id}",
                    "zones": {},
                    "night_cooling": {},
                    "ventilation_events": [],
                    "hydraulic_balance": [],
                    "anomalies": [],
                }
            target_zones = [resolved]
        else:
            target_zones = self._topology.monitored_zones()

        existing_models = await self._store.get_all_zone_models()

        computed = await asyncio.to_thread(
            self._analyze_history_sync,
            history_by_entity,
            target_zones,
            existing_models,
            now,
        )

        updated_models: list[ThermalZoneModelRecord] = computed["models_to_upsert"]
        observations: list[ThermalObservationRecord] = computed["observations_to_record"]

        for rec in updated_models:
            await self._store.upsert_zone_model(rec)
        for obs in observations:
            await self._store.record_observation(obs)

        return {
            "analyzed_at": now.astimezone(UTC).isoformat(),
            "zone_count": len(target_zones),
            "calibrations": computed["calibrations"],
            "night_cooling": computed["night_cooling"],
            "ventilation_events": computed["ventilation_events"],
            "warmup_rates_c_per_h": computed["warmup_rates_c_per_h"],
            "hydraulic_balance": computed["hydraulic_balance"],
            "anomalies": computed["anomalies"],
            "persisted_models": len(updated_models),
            "persisted_observations": len(observations),
        }

    def _extract_outdoor_history(
        self,
        parsed_by_entity: Mapping[str, list[_HistoryPoint]],
    ) -> tuple[list[tuple[datetime, float]], list[tuple[datetime, float]]]:
        temp_series: list[tuple[datetime, float]] = []
        hum_series: list[tuple[datetime, float]] = []

        temp_cands: list[str] = []
        if self._settings.outdoor_temp_entity:
            temp_cands.append(self._settings.outdoor_temp_entity)
        temp_cands.extend(self._topology.outdoor_temp_sensors)
        if self._settings.outdoor_weather_entity:
            temp_cands.append(self._settings.outdoor_weather_entity)
        if self._topology.weather_entity:
            temp_cands.append(self._topology.weather_entity)

        for eid in temp_cands:
            pts = parsed_by_entity.get(eid)
            if not pts:
                continue
            cand: list[tuple[datetime, float]] = []
            for pt in pts:
                val = (
                    _parse_float(pt.attrs.get("temperature"))
                    if eid.startswith("weather.")
                    else (
                        pt.numeric_val
                        if pt.numeric_val is not None
                        else _parse_float(pt.attrs.get("temperature"))
                    )
                )
                if val is not None:
                    cand.append((pt.ts, val))
            if cand:
                temp_series = cand
                break

        hum_cands: list[str] = []
        if self._settings.outdoor_weather_entity:
            hum_cands.append(self._settings.outdoor_weather_entity)
        if self._topology.weather_entity:
            hum_cands.append(self._topology.weather_entity)
        hum_cands.extend(temp_cands)

        for eid in hum_cands:
            pts = parsed_by_entity.get(eid)
            if not pts:
                continue
            cand_h: list[tuple[datetime, float]] = []
            for pt in pts:
                val = sanitize_humidity(_parse_float(pt.attrs.get("humidity")))
                if val is None and eid.startswith("sensor.") and "humidity" in eid.lower():
                    val = sanitize_humidity(pt.numeric_val)
                if val is not None:
                    cand_h.append((pt.ts, val))
            if cand_h:
                hum_series = cand_h
                break

        return temp_series, hum_series

    @staticmethod
    def _build_zone_series(
        zone: ZoneTopology,
        parsed_by_entity: Mapping[str, list[_HistoryPoint]],
    ) -> dict[str, Any]:
        etrv_series: list[tuple[datetime, float]] = []
        ambient_series: list[tuple[datetime, float]] = []
        hum_series: list[tuple[datetime, float]] = []

        # Pick longest ambient sensor and longest eTRV sensor (or combine averages when aligned)
        ambient_cands: list[tuple[float, list[tuple[datetime, float]]]] = []
        etrv_cands: list[list[tuple[datetime, float]]] = []

        for sp in zone.temperature_sensors:
            pts = parsed_by_entity.get(sp.entity_id)
            if not pts:
                continue
            series: list[tuple[datetime, float]] = []
            for pt in pts:
                if sp.entity_id.startswith("climate."):
                    val = _parse_float(pt.attrs.get("current_temperature"))
                else:
                    val = (
                        pt.numeric_val
                        if pt.numeric_val is not None
                        else _parse_float(
                            pt.attrs.get("temperature") or pt.attrs.get("current_temperature")
                        )
                    )
                if val is not None:
                    series.append((pt.ts, val))
            if not series:
                continue
            if sp.placement == "on_radiator":
                etrv_cands.append(series)
            elif sp.placement == "ambient_room":
                ambient_cands.append((sp.reliability_weight, series))
            else:
                ambient_cands.append((0.6, series))

        if etrv_cands:
            etrv_cands.sort(key=len, reverse=True)
            etrv_series = etrv_cands[0]
        if ambient_cands:
            ambient_cands.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
            ambient_series = ambient_cands[0][1]

        for heid in zone.humidity_sensors:
            pts = parsed_by_entity.get(heid)
            if not pts:
                continue
            cand_h: list[tuple[datetime, float]] = []
            for pt in pts:
                val = sanitize_humidity(pt.numeric_val)
                if val is None:
                    val = sanitize_humidity(_parse_float(pt.attrs.get("humidity")))
                if val is not None:
                    cand_h.append((pt.ts, val))
            if cand_h:
                hum_series = cand_h
                break
        if not hum_series:
            for cid in zone.climate_entities:
                pts = parsed_by_entity.get(cid)
                if not pts:
                    continue
                cand_h = []
                for pt in pts:
                    val = sanitize_humidity(_parse_float(pt.attrs.get("current_humidity")))
                    if val is not None:
                        cand_h.append((pt.ts, val))
                if cand_h:
                    hum_series = cand_h
                    break

        valve_points: list[_HistoryPoint] = []
        for veid in zone.valve_position_sensors:
            pts = parsed_by_entity.get(veid)
            if pts:
                valve_points = pts
                break

        ordered_climates = [c for c in zone.climate_entities if not _is_ac_climate_id(c)] + [
            c for c in zone.climate_entities if _is_ac_climate_id(c)
        ]
        climate_points: list[_HistoryPoint] = []
        for cid in ordered_climates:
            pts = parsed_by_entity.get(cid)
            if pts:
                climate_points = pts
                break

        return {
            "etrv_series": etrv_series,
            "ambient_series": ambient_series,
            "hum_series": hum_series,
            "valve_points": valve_points,
            "climate_points": climate_points,
        }

    @staticmethod
    def _state_at(
        t: datetime,
        zone: ZoneTopology,
        zdata: Mapping[str, Any],
        parsed_by_entity: Mapping[str, list[_HistoryPoint]],
    ) -> tuple[float | None, str | None, float | None, bool]:
        """Return ``(valve_pct, hvac_action, target_c, window_open)`` at timestamp ``t``."""
        v_pt = _lookup_step(zdata["valve_points"], t)
        c_pt = _lookup_step(zdata["climate_points"], t)

        hvac_action: str | None = None
        target_c: float | None = None
        c_valve: float | None = None
        if c_pt is not None:
            raw_act = c_pt.attrs.get("hvac_action")
            if isinstance(raw_act, str) and raw_act.strip():
                hvac_action = raw_act.strip().lower()
            elif c_pt.state_str.lower() == "off":
                hvac_action = "off"
            target_c = _parse_float(c_pt.attrs.get("temperature"))
            c_val = _parse_float(c_pt.attrs.get("valve_position"))
            if c_val is not None and 0.0 <= c_val <= 100.0:
                c_valve = c_val

        valve_pct: float | None = None
        if v_pt is not None and v_pt.numeric_val is not None and 0.0 <= v_pt.numeric_val <= 100.0:
            valve_pct = v_pt.numeric_val
        elif c_valve is not None:
            valve_pct = c_valve
        elif hvac_action in ("idle", "off", "cooling"):
            valve_pct = 0.0
        elif hvac_action == "heating":
            valve_pct = 60.0

        window_open = False
        for ceid in zone.contact_sensors:
            c_state = _lookup_step(parsed_by_entity.get(ceid, []), t)
            if c_state is not None and c_state.state_str.lower() in _OPEN_STATES:
                window_open = True
                break

        return valve_pct, hvac_action, target_c, window_open

    def _analyze_history_sync(
        self,
        history_by_entity: Mapping[str, Sequence[Mapping[str, Any]]],
        target_zones: Sequence[ZoneTopology],
        existing_models: Mapping[str, ThermalZoneModelRecord],
        now_utc: datetime,
    ) -> dict[str, Any]:
        parsed_by_entity: dict[str, list[_HistoryPoint]] = {
            eid: _extract_history_points(recs) for eid, recs in history_by_entity.items()
        }
        outdoor_temp_series, outdoor_hum_series = self._extract_outdoor_history(parsed_by_entity)

        calibrations: dict[str, dict[str, Any]] = {}
        night_cooling_map: dict[str, dict[str, Any]] = {}
        ventilation_events: list[dict[str, Any]] = []
        warmup_rates: dict[str, float] = {}
        anomalies: list[dict[str, Any]] = []
        models_to_upsert: list[ThermalZoneModelRecord] = []
        observations_to_record: list[ThermalObservationRecord] = []

        zone_data_cache: dict[str, dict[str, Any]] = {}
        zone_k_map: dict[str, float] = {}
        zone_cool_map: dict[str, float | None] = {}
        stalled_zones: set[str] = set()

        for zone in target_zones:
            zdata = self._build_zone_series(zone, parsed_by_entity)
            zone_data_cache[zone.zone_id] = zdata

            prev_model = existing_models.get(zone.zone_id)
            k_loss = prev_model.k_loss if prev_model is not None else 0.08
            etrv_bias = (
                prev_model.etrv_bias_factor
                if prev_model is not None
                else self._settings.default_etrv_bias_factor
            )
            static_offset = prev_model.static_offset_c if prev_model is not None else 0.0
            avg_warmup = prev_model.avg_warmup_rate_c_per_h if prev_model is not None else None
            avg_cooling = prev_model.avg_cooling_rate_c_per_h if prev_model is not None else None

            etrv_series: list[tuple[datetime, float]] = zdata["etrv_series"]
            ambient_series: list[tuple[datetime, float]] = zdata["ambient_series"]

            # 1. Self-Calibration (Static & Dynamic eTRV bias)
            static_updates = 0
            dynamic_updates = 0
            if etrv_series and ambient_series:
                timeline_ts = sorted(
                    {ts for ts, _ in etrv_series}
                    | {ts for ts, _ in ambient_series}
                    | {pt.ts for pt in zdata["valve_points"]}
                    | {pt.ts for pt in zdata["climate_points"]}
                )
                alpha = self._settings.ema_alpha
                idle_start: datetime | None = None
                last_static_ts: datetime | None = None
                last_dyn_ts: datetime | None = None

                for ts in timeline_ts:
                    etrv_val = _interpolate_series(
                        etrv_series, ts, max_gap_sec=3600.0, edge_tol_sec=900.0
                    )
                    amb_val = _interpolate_series(
                        ambient_series, ts, max_gap_sec=3600.0, edge_tol_sec=900.0
                    )
                    if etrv_val is None or amb_val is None:
                        continue
                    valve_pct, hvac_action, _, _ = self._state_at(ts, zone, zdata, parsed_by_entity)
                    if valve_pct is not None and valve_pct <= 1.0 and hvac_action != "heating":
                        if idle_start is None:
                            idle_start = ts
                        idle_min = (ts - idle_start).total_seconds() / 60.0
                        if idle_min >= float(self._settings.static_calibration_idle_minutes) and (
                            last_static_ts is None or (ts - last_static_ts).total_seconds() >= 900.0
                        ):
                            observed_offset = etrv_val - amb_val
                            static_offset = (1.0 - alpha) * static_offset + alpha * observed_offset
                            last_static_ts = ts
                            static_updates += 1
                    else:
                        idle_start = None

                    if (
                        valve_pct is not None
                        and valve_pct > self._settings.dynamic_calibration_min_valve_pct
                        and (last_dyn_ts is None or (ts - last_dyn_ts).total_seconds() >= 900.0)
                    ):
                        excess = etrv_val - amb_val - static_offset
                        observed_bias = excess / (valve_pct / 100.0)
                        etrv_bias = max(
                            0.0,
                            min(8.0, (1.0 - alpha) * etrv_bias + alpha * observed_bias),
                        )
                        last_dyn_ts = ts
                        dynamic_updates += 1

            calibrations[zone.zone_id] = {
                "static_offset_c": round(static_offset, 4),
                "etrv_bias_factor": round(etrv_bias, 4),
                "static_updates": static_updates,
                "dynamic_updates": dynamic_updates,
            }

            # Construct primary indoor temperature series (ambient preferred, else corrected eTRV)
            primary_temp_series: list[tuple[datetime, float]] = list(ambient_series)
            if not primary_temp_series and etrv_series:
                for ts, raw_t in etrv_series:
                    valve_pct, _, _, _ = self._state_at(ts, zone, zdata, parsed_by_entity)
                    v_eff = valve_pct if valve_pct is not None else 0.0
                    primary_temp_series.append((ts, raw_t - static_offset - etrv_bias * (v_eff / 100.0)))

            # 2. Nighttime Cooling Curve & k_loss
            night_runs: list[list[tuple[datetime, float]]] = []
            current_run: list[tuple[datetime, float]] = []
            for ts, temp_c in primary_temp_series:
                valve_pct, hvac_action, _, win_open = self._state_at(ts, zone, zdata, parsed_by_entity)
                is_night = _in_night_window(
                    ts,
                    self._settings.night_window_start_hour,
                    self._settings.night_window_end_hour,
                    tz=self._tz,
                )
                heating_off = (valve_pct is None or valve_pct <= 1.0) and hvac_action != "heating"
                if is_night and heating_off and not win_open:
                    if current_run and (ts - current_run[-1][0]).total_seconds() > 9000.0:
                        night_runs.append(current_run)
                        current_run = [(ts, temp_c)]
                    else:
                        current_run.append((ts, temp_c))
                else:
                    if current_run:
                        night_runs.append(current_run)
                        current_run = []
            if current_run:
                night_runs.append(current_run)

            valid_runs = [
                r for r in night_runs if len(r) >= 3 and (r[-1][0] - r[0][0]).total_seconds() >= 1800.0
            ]
            if valid_runs:
                best_run = max(valid_runs, key=lambda r: ((r[-1][0] - r[0][0]).total_seconds(), len(r)))
                t0_dt = best_run[0][0]
                t_arr = np.array(
                    [(pt_ts - t0_dt).total_seconds() / 3600.0 for pt_ts, _ in best_run],
                    dtype=np.float64,
                )
                y_arr = np.array([pt_val for _, pt_val in best_run], dtype=np.float64)
                out_vals = [
                    v
                    for pt_ts, _ in best_run
                    if (v := _interpolate_series(outdoor_temp_series, pt_ts)) is not None
                ]
                out_mean: float | None = (sum(out_vals) / len(out_vals)) if out_vals else None
                t_out_fit = out_mean if out_mean is not None else min(5.0, float(np.min(y_arr)) - 5.0)

                k_fit, r2, method = _fit_newton_cooling(t_arr, y_arr, t_out_fit)
                k_loss = k_fit
                duration_h = max(float(t_arr[-1]), 1e-6)
                cooling_rate = round((float(y_arr[0]) - float(y_arr[-1])) / duration_h, 4)
                avg_cooling = cooling_rate

                nc = NightCoolingAnalysis(
                    zone_id=zone.zone_id,
                    name=zone.name,
                    started_at=best_run[0][0].isoformat(),
                    ended_at=best_run[-1][0].isoformat(),
                    duration_min=round(duration_h * 60.0, 2),
                    sample_count=len(best_run),
                    t_start_c=round(float(y_arr[0]), 3),
                    t_min_c=round(float(np.min(y_arr)), 3),
                    t_end_c=round(float(y_arr[-1]), 3),
                    outdoor_temp_mean_c=round(out_mean, 2) if out_mean is not None else None,
                    k_loss=round(k_fit, 5),
                    tau_hours=round(1.0 / max(k_fit, 1e-4), 2),
                    cooling_rate_c_per_h=cooling_rate,
                    r_squared=round(r2, 4),
                    fit_method=method,
                )
                night_cooling_map[zone.zone_id] = nc.model_dump()
                observations_to_record.append(
                    ThermalObservationRecord(
                        zone_id=zone.zone_id,
                        event_type="night_cooling",
                        started_at=nc.started_at,
                        duration_min=nc.duration_min,
                        outdoor_temp_c=nc.outdoor_temp_mean_c,
                        outdoor_humidity=None,
                        indoor_temp_start_c=nc.t_start_c,
                        indoor_temp_end_c=nc.t_end_c,
                        metadata={
                            "k_loss": nc.k_loss,
                            "tau_hours": nc.tau_hours,
                            "cooling_rate_c_per_h": nc.cooling_rate_c_per_h,
                            "r_squared": nc.r_squared,
                            "fit_method": nc.fit_method,
                        },
                    )
                )

            # 3. Ventilation Shock & Rebound Analyzer
            hum_series: list[tuple[datetime, float]] = zdata["hum_series"]
            for ceid in zone.contact_sensors:
                c_pts = parsed_by_entity.get(ceid, [])
                if not c_pts:
                    continue
                open_ts: datetime | None = None
                for idx, pt in enumerate(c_pts):
                    is_open = pt.state_str.lower() in _OPEN_STATES
                    if is_open and open_ts is None:
                        open_ts = pt.ts
                    elif not is_open and open_ts is not None:
                        close_ts = pt.ts
                        dur_min = (close_ts - open_ts).total_seconds() / 60.0
                        if dur_min >= 0.5 and primary_temp_series:
                            next_open_ts: datetime | None = None
                            for nxt in c_pts[idx + 1 :]:
                                if nxt.state_str.lower() in _OPEN_STATES:
                                    next_open_ts = nxt.ts
                                    break
                            v_evt = self._analyze_ventilation_episode(
                                zone=zone,
                                contact_entity=ceid,
                                open_ts=open_ts,
                                close_ts=close_ts,
                                next_open_ts=next_open_ts,
                                temp_series=primary_temp_series,
                                hum_series=hum_series,
                                outdoor_temp_series=outdoor_temp_series,
                                outdoor_hum_series=outdoor_hum_series,
                                zdata=zdata,
                                parsed_by_entity=parsed_by_entity,
                            )
                            if v_evt is not None:
                                ventilation_events.append(v_evt.model_dump())
                                observations_to_record.append(
                                    ThermalObservationRecord(
                                        zone_id=zone.zone_id,
                                        event_type="ventilation_shock",
                                        started_at=v_evt.started_at,
                                        duration_min=v_evt.duration_min,
                                        outdoor_temp_c=v_evt.outdoor_temp_c,
                                        outdoor_humidity=v_evt.outdoor_humidity_pct,
                                        indoor_temp_start_c=v_evt.indoor_temp_start_c,
                                        indoor_temp_end_c=v_evt.indoor_temp_end_c,
                                        indoor_abs_humidity_drop=v_evt.indoor_abs_humidity_drop,
                                        rebound_temp_c=v_evt.rebound_temp_c,
                                        metadata={
                                            "contact_entity": ceid,
                                            "enthalpy_loss_kj": v_evt.enthalpy_loss_kj,
                                            "knee_point_min": v_evt.knee_point_min,
                                            "rebound_delta_c": v_evt.rebound_delta_c,
                                        },
                                    )
                                )
                        open_ts = None

            # 4. Warm-up Rate & Prolonged Heating Stall Anomaly
            if len(primary_temp_series) >= 2:
                heat_runs: list[list[tuple[datetime, float]]] = []
                cur_heat: list[tuple[datetime, float]] = []
                stall_start: datetime | None = None
                stall_start_temp: float | None = None
                stall_flagged = False

                for ts, temp_c in primary_temp_series:
                    valve_pct, hvac_action, target_c, win_open = self._state_at(
                        ts, zone, zdata, parsed_by_entity
                    )
                    is_heating = not win_open and (
                        (valve_pct is not None and valve_pct > 50.0) or hvac_action == "heating"
                    )
                    if is_heating:
                        if cur_heat and (ts - cur_heat[-1][0]).total_seconds() > 5400.0:
                            heat_runs.append(cur_heat)
                            cur_heat = [(ts, temp_c)]
                        else:
                            cur_heat.append((ts, temp_c))
                    else:
                        if cur_heat:
                            heat_runs.append(cur_heat)
                            cur_heat = []

                    # Stall anomaly check
                    high_valve = (
                        not win_open
                        and (target_c is None or target_c > 8.0)
                        and valve_pct is not None
                        and valve_pct >= self._settings.anomaly_valve_open_pct
                    )
                    if high_valve:
                        if stall_start is None:
                            stall_start = ts
                            stall_start_temp = temp_c
                        else:
                            stall_dur_min = (ts - stall_start).total_seconds() / 60.0
                            if (
                                stall_dur_min >= float(self._settings.anomaly_min_duration_min)
                                and stall_start_temp is not None
                                and (temp_c - stall_start_temp) <= 0.1
                                and not stall_flagged
                            ):
                                stall_flagged = True
                                stalled_zones.add(zone.zone_id)
                                anomalies.append(
                                    {
                                        "zone_id": zone.zone_id,
                                        "name": zone.name,
                                        "type": "heating_stall",
                                        "started_at": stall_start.isoformat(),
                                        "ended_at": ts.isoformat(),
                                        "duration_min": round(stall_dur_min, 1),
                                        "delta_temp_c": round(temp_c - stall_start_temp, 3),
                                        "valve_pct": round(valve_pct, 1)
                                        if valve_pct is not None
                                        else None,
                                    }
                                )
                    else:
                        stall_start = None
                        stall_start_temp = None

                if cur_heat:
                    heat_runs.append(cur_heat)

                weighted_sum = 0.0
                weight_total = 0.0
                for run in heat_runs:
                    dur_h = (run[-1][0] - run[0][0]).total_seconds() / 3600.0
                    if len(run) >= 2 and dur_h >= 0.25:
                        rate = (run[-1][1] - run[0][1]) / dur_h
                        if rate > 0.0:
                            weighted_sum += rate * dur_h
                            weight_total += dur_h
                if weight_total > 0.0:
                    avg_warmup = round(weighted_sum / weight_total, 4)
                    warmup_rates[zone.zone_id] = avg_warmup

            zone_k_map[zone.zone_id] = k_loss
            zone_cool_map[zone.zone_id] = avg_cooling
            if zone.zone_id not in warmup_rates and avg_warmup is not None:
                warmup_rates[zone.zone_id] = avg_warmup

            models_to_upsert.append(
                ThermalZoneModelRecord(
                    zone_id=zone.zone_id,
                    volume_m3=zone.volume_m3,
                    k_loss=round(k_loss, 5),
                    etrv_bias_factor=round(etrv_bias, 4),
                    static_offset_c=round(static_offset, 4),
                    avg_warmup_rate_c_per_h=warmup_rates.get(zone.zone_id),
                    avg_cooling_rate_c_per_h=avg_cooling,
                    updated_at=now_utc.astimezone(UTC).isoformat(),
                )
            )

        hydraulic_balance = self._build_hydraulic_comparison(
            zones=target_zones,
            warmup_rates=warmup_rates,
            cooling_rates=zone_cool_map,
            k_losses=zone_k_map,
            stalled_zones=stalled_zones,
        )

        return {
            "calibrations": calibrations,
            "night_cooling": night_cooling_map,
            "ventilation_events": ventilation_events,
            "warmup_rates_c_per_h": warmup_rates,
            "hydraulic_balance": [item.model_dump() for item in hydraulic_balance],
            "anomalies": anomalies,
            "models_to_upsert": models_to_upsert,
            "observations_to_record": observations_to_record,
        }

    def _analyze_ventilation_episode(
        self,
        zone: ZoneTopology,
        contact_entity: str,
        open_ts: datetime,
        close_ts: datetime,
        next_open_ts: datetime | None,
        temp_series: Sequence[tuple[datetime, float]],
        hum_series: Sequence[tuple[datetime, float]],
        outdoor_temp_series: Sequence[tuple[datetime, float]],
        outdoor_hum_series: Sequence[tuple[datetime, float]],
        zdata: Mapping[str, Any],
        parsed_by_entity: Mapping[str, list[_HistoryPoint]],
    ) -> VentilationShockEvent | None:
        t_start = _interpolate_series(temp_series, open_ts)
        t_end = _interpolate_series(temp_series, close_ts)
        if t_start is None or t_end is None:
            return None

        duration_min = (close_ts - open_ts).total_seconds() / 60.0
        out_temp = _interpolate_series(outdoor_temp_series, open_ts)
        out_rh = sanitize_humidity(_interpolate_series(outdoor_hum_series, open_ts))
        rh_start = sanitize_humidity(_interpolate_series(hum_series, open_ts))
        rh_end = sanitize_humidity(_interpolate_series(hum_series, close_ts))

        ah_start = calculate_absolute_humidity(t_start, rh_start) if rh_start is not None else None
        ah_end = calculate_absolute_humidity(t_end, rh_end) if rh_end is not None else None
        ah_drop = round(ah_start - ah_end, 4) if (ah_start is not None and ah_end is not None) else None

        m_air_kg = 1.2 * max(zone.volume_m3, 1.0)
        if rh_start is not None and rh_end is not None:
            enthalpy_loss_kj = round(
                m_air_kg * (calculate_enthalpy(t_start, rh_start) - calculate_enthalpy(t_end, rh_end)),
                2,
            )
        else:
            enthalpy_loss_kj = round(m_air_kg * 1.006 * (t_start - t_end), 2)

        # Post-closure rebound before heating resumes
        post_limit = close_ts + timedelta(minutes=45)
        if next_open_ts is not None and next_open_ts < post_limit:
            post_limit = next_open_ts
        post_temps: list[float] = []
        for ts, val in temp_series:
            if ts <= close_ts:
                continue
            if ts > post_limit:
                break
            valve_pct, hvac_action, _, _ = self._state_at(ts, zone, zdata, parsed_by_entity)
            if hvac_action == "heating" or (valve_pct is not None and valve_pct > 5.0):
                break
            post_temps.append(val)

        rebound_temp_c = round(max(post_temps), 3) if post_temps else None
        rebound_delta_c = (
            round(max(0.0, rebound_temp_c - t_end), 3) if rebound_temp_c is not None else None
        )

        # Knee-point detection on absolute humidity trajectory during open interval
        knee_point_min: float | None = None
        if ah_start is not None and ah_end is not None:
            sample_times = sorted(
                {open_ts, close_ts}
                | {ts for ts, _ in hum_series if open_ts <= ts <= close_ts}
                | {ts for ts, _ in temp_series if open_ts <= ts <= close_ts}
            )
            ah_points: list[tuple[float, float]] = []
            for ts in sample_times:
                tv = _interpolate_series(temp_series, ts)
                hv = sanitize_humidity(_interpolate_series(hum_series, ts))
                if tv is not None and hv is not None:
                    ah_points.append(
                        ((ts - open_ts).total_seconds() / 60.0, calculate_absolute_humidity(tv, hv))
                    )
            if len(ah_points) >= 3:
                total_drop = ah_points[0][1] - ah_points[-1][1]
                out_ah = (
                    calculate_absolute_humidity(out_temp, out_rh)
                    if (out_temp is not None and out_rh is not None)
                    else None
                )
                eq_drop = (
                    (ah_points[0][1] - out_ah)
                    if (out_ah is not None and out_ah < ah_points[0][1])
                    else total_drop
                )
                if total_drop > 0.05:
                    rates: list[float] = []
                    for i in range(1, len(ah_points)):
                        dt_m = max(ah_points[i][0] - ah_points[i - 1][0], 1e-3)
                        rates.append((ah_points[i - 1][1] - ah_points[i][1]) / dt_m)
                    init_rate = rates[0] if rates[0] > 0.0 else max(rates)
                    target_scale = max(eq_drop, total_drop, 1e-6)
                    for i in range(1, len(ah_points)):
                        cum_drop = ah_points[0][1] - ah_points[i][1]
                        frac_eq = cum_drop / target_scale
                        frac_obs = cum_drop / max(total_drop, 1e-6)
                        if frac_eq >= 0.85 or frac_obs >= 0.85:
                            knee_point_min = round(ah_points[i][0], 2)
                            break
                        if (
                            init_rate > 0.0
                            and cum_drop >= 0.30 * total_drop
                            and rates[i - 1] <= 0.15 * init_rate
                        ):
                            knee_point_min = round(ah_points[i][0], 2)
                            break
                    if knee_point_min is None:
                        knee_point_min = round(duration_min, 2)

        return VentilationShockEvent(
            zone_id=zone.zone_id,
            name=zone.name,
            contact_entity=contact_entity,
            started_at=open_ts.isoformat(),
            ended_at=close_ts.isoformat(),
            duration_min=round(duration_min, 2),
            indoor_temp_start_c=round(t_start, 3),
            indoor_temp_end_c=round(t_end, 3),
            indoor_temp_drop_c=round(t_start - t_end, 3),
            outdoor_temp_c=round(out_temp, 2) if out_temp is not None else None,
            outdoor_humidity_pct=round(out_rh, 2) if out_rh is not None else None,
            indoor_humidity_start_pct=round(rh_start, 2) if rh_start is not None else None,
            indoor_humidity_end_pct=round(rh_end, 2) if rh_end is not None else None,
            indoor_abs_humidity_start_g_m3=round(ah_start, 3) if ah_start is not None else None,
            indoor_abs_humidity_end_g_m3=round(ah_end, 3) if ah_end is not None else None,
            indoor_abs_humidity_drop=ah_drop,
            enthalpy_loss_kj=enthalpy_loss_kj,
            rebound_temp_c=rebound_temp_c,
            rebound_delta_c=rebound_delta_c,
            knee_point_min=knee_point_min,
        )

    @staticmethod
    def _build_hydraulic_comparison(
        zones: Sequence[ZoneTopology],
        warmup_rates: Mapping[str, float],
        cooling_rates: Mapping[str, float | None],
        k_losses: Mapping[str, float],
        stalled_zones: set[str],
    ) -> list[HydraulicZoneComparison]:
        positive_rates = [r for r in warmup_rates.values() if r > 0.0]
        median_rate = float(statistics.median(positive_rates)) if positive_rates else 0.0

        results: list[HydraulicZoneComparison] = []
        for zone in zones:
            w_rate = warmup_rates.get(zone.zone_id)
            c_rate = cooling_rates.get(zone.zone_id)
            k_val = k_losses.get(zone.zone_id, 0.08)
            stalled = zone.zone_id in stalled_zones
            rel_ratio: float | None = None
            classification: Literal[
                "fast_heating",
                "balanced",
                "slow_heating",
                "stalled",
                "insufficient_data",
            ] = "insufficient_data"
            rec_max_valve: int | None = None
            notes = ""

            if stalled:
                classification = "stalled"
                notes = (
                    "Sustained open valve with minimal temperature rise; "
                    "inspect valve pin or circuit flow."
                )
            elif w_rate is not None and median_rate > 0.0:
                rel_ratio = round(w_rate / median_rate, 3)
                if rel_ratio >= 1.35:
                    classification = "fast_heating"
                    rec_max_valve = int(max(40, min(85, round((100.0 / rel_ratio) / 5.0) * 5)))
                    notes = (
                        f"Heats {rel_ratio:.2f}x faster than median; consider throttling "
                        f"max_valve_position to {rec_max_valve}% to balance boiler return flow."
                    )
                elif rel_ratio <= 0.70:
                    classification = "slow_heating"
                    notes = (
                        f"Heats slower than median ({rel_ratio:.2f}x); keep full valve stroke available."
                    )
                else:
                    classification = "balanced"
                    notes = "Warm-up rate is balanced relative to other zones."

            results.append(
                HydraulicZoneComparison(
                    zone_id=zone.zone_id,
                    name=zone.name,
                    volume_m3=zone.volume_m3,
                    avg_warmup_rate_c_per_h=w_rate,
                    avg_cooling_rate_c_per_h=c_rate,
                    k_loss=round(k_val, 5),
                    relative_speed_ratio=rel_ratio,
                    classification=classification,
                    max_valve_entity=zone.max_valve_entity,
                    recommended_max_valve_pct=rec_max_valve,
                    anomaly_detected=stalled,
                    notes=notes,
                )
            )

        results.sort(
            key=lambda item: (
                item.avg_warmup_rate_c_per_h is not None,
                item.avg_warmup_rate_c_per_h or 0.0,
            ),
            reverse=True,
        )
        return results

    async def simulate_scenario(
        self,
        live_snapshot: ThermalSnapshotSummary | None = None,
        zone_id: str | None = None,
        outdoor_temp_curve_c: Sequence[float] | None = None,
        outdoor_temp_c: float | None = None,
        outdoor_humidity_pct: float | None = None,
        hours_ahead: float = 8.0,
        target_setpoint_c: float | None = None,
    ) -> dict[str, Any]:
        """Simulate overnight cooling trajectories, warm-up times, and hydraulic balance."""
        if hours_ahead <= 0.0:
            raise ValueError("hours_ahead must be positive")

        if zone_id is not None:
            resolved = self._topology.resolve_zone(zone_id)
            if resolved is None:
                return {"error": f"Unknown zone: {zone_id}", "projections": {}, "hydraulic_balance": []}
            target_zones = [resolved]
        else:
            target_zones = self._topology.monitored_zones()

        models = await self._store.get_all_zone_models()

        n_steps = max(1, math.ceil(hours_ahead))
        base_out = (
            outdoor_temp_c
            if outdoor_temp_c is not None
            else (
                live_snapshot.outdoor_temp_c
                if live_snapshot and live_snapshot.outdoor_temp_c is not None
                else 5.0
            )
        )
        if outdoor_temp_curve_c:
            curve = [float(v) for v in outdoor_temp_curve_c]
            while len(curve) < n_steps:
                curve.append(curve[-1])
            curve = curve[:n_steps]
        else:
            curve = [float(base_out)] * n_steps

        eff_out_rh = sanitize_humidity(
            outdoor_humidity_pct
            if outdoor_humidity_pct is not None
            else (live_snapshot.outdoor_humidity_pct if live_snapshot else None)
        )
        eff_out_ah = (
            calculate_absolute_humidity(curve[0], eff_out_rh) if eff_out_rh is not None else None
        )

        projections: dict[str, dict[str, Any]] = {}
        coldest_zone_id: str | None = None
        coldest_temp = math.inf

        warmup_rates: dict[str, float] = {}
        cooling_rates: dict[str, float | None] = {}
        k_losses: dict[str, float] = {}
        stalled_zones: set[str] = set()

        for zone in self._topology.monitored_zones():
            model = models.get(zone.zone_id)
            k_losses[zone.zone_id] = model.k_loss if model is not None else 0.08
            cooling_rates[zone.zone_id] = model.avg_cooling_rate_c_per_h if model is not None else None
            if model is not None and model.avg_warmup_rate_c_per_h is not None:
                warmup_rates[zone.zone_id] = model.avg_warmup_rate_c_per_h
            if (
                live_snapshot
                and (live_z := live_snapshot.zones.get(zone.zone_id))
                and live_z.anomaly_detected
            ):
                stalled_zones.add(zone.zone_id)

        for zone in target_zones:
            live_z = live_snapshot.zones.get(zone.zone_id) if live_snapshot else None
            model = models.get(zone.zone_id)
            k_loss = model.k_loss if model is not None else (live_z.k_loss if live_z else 0.08)

            t_curr = (
                live_z.true_temp_c if (live_z is not None and live_z.true_temp_c is not None) else 20.0
            )
            hourly_temps: list[float] = []
            remaining = hours_ahead
            t_sim = t_curr
            for step_out in curve:
                dt = min(1.0, max(0.0, remaining))
                if dt <= 0.0:
                    break
                t_sim = step_out + (t_sim - step_out) * math.exp(-k_loss * dt)
                hourly_temps.append(round(t_sim, 2))
                remaining -= dt

            proj_min = min(hourly_temps) if hourly_temps else t_curr
            if proj_min < coldest_temp:
                coldest_temp = proj_min
                coldest_zone_id = zone.zone_id

            tgt = (
                target_setpoint_c
                if target_setpoint_c is not None
                else (
                    live_z.target_setpoint_c
                    if (live_z and live_z.target_setpoint_c is not None)
                    else 21.0
                )
            )
            deficit = max(0.0, tgt - proj_min)
            w_rate = model.avg_warmup_rate_c_per_h if (model and model.avg_warmup_rate_c_per_h) else None
            eff_w_rate = w_rate if (w_rate is not None and w_rate > 0.0) else 1.5
            est_warmup_min = round((deficit / eff_w_rate) * 60.0, 1) if deficit > 0.0 else 0.0

            rec_vent_min = _compute_optimal_ventilation_min(
                volume_m3=zone.volume_m3,
                window_area_m2=zone.window_area_m2,
                indoor_temp_c=t_curr,
                outdoor_temp_c=curve[0],
                indoor_abs_humidity_g_m3=live_z.absolute_humidity_g_m3 if live_z else None,
                outdoor_abs_humidity_g_m3=eff_out_ah,
                indoor_rh_pct=live_z.rel_humidity_pct if live_z else None,
            )

            proj = ScenarioProjection(
                zone_id=zone.zone_id,
                name=zone.name,
                current_temp_c=round(t_curr, 2),
                k_loss=round(k_loss, 5),
                hours_ahead=hours_ahead,
                hourly_temps_c=hourly_temps,
                projected_min_temp_c=round(proj_min, 2),
                target_setpoint_c=round(tgt, 2),
                estimated_warmup_min=est_warmup_min,
                recommended_ventilation_min=rec_vent_min,
            )
            projections[zone.zone_id] = proj.model_dump()

        hydraulic_balance = self._build_hydraulic_comparison(
            zones=self._topology.monitored_zones(),
            warmup_rates=warmup_rates,
            cooling_rates=cooling_rates,
            k_losses=k_losses,
            stalled_zones=stalled_zones,
        )

        return {
            "hours_ahead": hours_ahead,
            "outdoor_temp_curve_c": curve,
            "coldest_zone_id": coldest_zone_id,
            "coldest_projected_temp_c": round(coldest_temp, 2) if math.isfinite(coldest_temp) else None,
            "projections": projections,
            "hydraulic_balance": [item.model_dump() for item in hydraulic_balance],
        }
