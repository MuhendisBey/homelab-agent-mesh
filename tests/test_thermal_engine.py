"""Unit tests for psychrometrics, SQLite ThermalStore, and ThermalPhysicsEngine."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.core.config import ThermalSettings
from src.core.neonplan_adapter import (
    BuildingTopology,
    RadiatorPlacement,
    SensorPlacement,
    ZoneTopology,
)
from src.core.psychrometrics import (
    calculate_absolute_humidity,
    calculate_dew_point,
    calculate_enthalpy,
    is_valid_humidity,
    sanitize_humidity,
)
from src.core.thermal_engine import ThermalPhysicsEngine
from src.memory.thermal_store import ThermalStore, ThermalZoneModelRecord


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _build_synthetic_topology() -> BuildingTopology:
    living = ZoneTopology(
        zone_id="living_room",
        room_id="r_living",
        name="Living Room",
        floor_id="f0",
        floor_name="Ground Floor",
        area_id="living_room",
        floor_area_m2=28.0,
        ceiling_height_m=2.6,
        volume_m3=72.8,
        window_area_m2=4.2,
        radiators=[RadiatorPlacement(id="rad_liv", x=1.5, z=0.2, climate_entity="climate.living_room_trv")],
        climate_entities=["climate.living_room_trv"],
        temperature_sensors=[
            SensorPlacement(
                entity_id="climate.living_room_trv",
                sensor_type="temperature",
                x=1.5,
                z=0.2,
                distance_to_radiator_m=0.0,
                placement="on_radiator",
                radiation_bias_factor=2.5,
                reliability_weight=1.0,
                source="radiator_climate",
            ),
            SensorPlacement(
                entity_id="sensor.living_room_ambient_temp",
                sensor_type="temperature",
                x=3.5,
                z=3.0,
                distance_to_radiator_m=3.44,
                placement="ambient_room",
                radiation_bias_factor=0.0,
                reliability_weight=1.0,
                source="neonplan",
            ),
        ],
        humidity_sensors=["sensor.living_room_humidity"],
        valve_position_sensors=["sensor.living_room_valve"],
        contact_sensors=["binary_sensor.living_room_window"],
        max_valve_entity="number.living_room_max_valve",
    )
    bedroom = ZoneTopology(
        zone_id="bedroom",
        room_id="r_bed",
        name="Bedroom",
        floor_id="f1",
        floor_name="First Floor",
        area_id="bedroom",
        floor_area_m2=16.0,
        ceiling_height_m=2.5,
        volume_m3=40.0,
        window_area_m2=2.4,
        radiators=[RadiatorPlacement(id="rad_bed", x=1.0, z=0.2, climate_entity="climate.bedroom_trv")],
        climate_entities=["climate.bedroom_trv"],
        temperature_sensors=[
            SensorPlacement(
                entity_id="climate.bedroom_trv",
                sensor_type="temperature",
                x=1.0,
                z=0.2,
                distance_to_radiator_m=0.0,
                placement="on_radiator",
                radiation_bias_factor=2.5,
                reliability_weight=1.0,
                source="radiator_climate",
            ),
            SensorPlacement(
                entity_id="sensor.bedroom_ambient_temp",
                sensor_type="temperature",
                x=2.5,
                z=2.5,
                distance_to_radiator_m=2.75,
                placement="ambient_room",
                radiation_bias_factor=0.0,
                reliability_weight=1.0,
                source="neonplan",
            ),
        ],
        humidity_sensors=["sensor.bedroom_humidity"],
        valve_position_sensors=["sensor.bedroom_valve"],
        contact_sensors=["binary_sensor.bedroom_window"],
        max_valve_entity="number.bedroom_max_valve",
    )
    study = ZoneTopology(
        zone_id="study",
        room_id="r_study",
        name="Study",
        floor_id="f1",
        floor_name="First Floor",
        area_id="study",
        floor_area_m2=10.0,
        ceiling_height_m=2.5,
        volume_m3=25.0,
        window_area_m2=1.5,
        radiators=[RadiatorPlacement(id="rad_study", x=1.0, z=0.2, climate_entity="climate.study_trv")],
        climate_entities=["climate.study_trv"],
        temperature_sensors=[
            SensorPlacement(
                entity_id="climate.study_trv",
                sensor_type="temperature",
                x=1.0,
                z=0.2,
                distance_to_radiator_m=0.0,
                placement="on_radiator",
                radiation_bias_factor=2.5,
                reliability_weight=1.0,
                source="radiator_climate",
            ),
            SensorPlacement(
                entity_id="sensor.study_ambient_temp",
                sensor_type="temperature",
                x=2.0,
                z=2.0,
                distance_to_radiator_m=2.06,
                placement="ambient_room",
                radiation_bias_factor=0.0,
                reliability_weight=1.0,
                source="neonplan",
            ),
        ],
        humidity_sensors=["sensor.study_humidity"],
        valve_position_sensors=["sensor.study_valve"],
        contact_sensors=["binary_sensor.study_window"],
        max_valve_entity="number.study_max_valve",
    )
    return BuildingTopology(
        weather_entity="weather.home",
        outdoor_temp_sensors=["sensor.outdoor_temperature"],
        zones={
            "living_room": living,
            "bedroom": bedroom,
            "study": study,
        },
    )


def test_psychrometrics_magnus_tetens_and_sentinel_rejection() -> None:
    """Verify thermodynamic formulas at (20C, 50% RH) and rejection of Bosch 255.0 / negative RH."""
    ah = calculate_absolute_humidity(20.0, 50.0)
    dp = calculate_dew_point(20.0, 50.0)
    enth = calculate_enthalpy(20.0, 50.0)

    assert ah == pytest.approx(8.65, abs=0.05)
    assert dp == pytest.approx(9.27, abs=0.05)
    assert enth == pytest.approx(38.6, abs=0.3)

    assert is_valid_humidity(50.0) is True
    assert is_valid_humidity(0.0) is True
    assert is_valid_humidity(100.0) is True
    assert is_valid_humidity(255.0) is False
    assert is_valid_humidity(-5.0) is False
    assert is_valid_humidity(None) is False
    assert is_valid_humidity(float("nan")) is False
    assert is_valid_humidity(True) is False

    assert sanitize_humidity(255.0) is None
    assert sanitize_humidity(-5.0) is None
    assert sanitize_humidity(61.5) == pytest.approx(61.5)

    with pytest.raises(ValueError):
        calculate_absolute_humidity(20.0, 255.0)
    with pytest.raises(ValueError):
        calculate_dew_point(20.0, -5.0)
    with pytest.raises(ValueError):
        calculate_enthalpy(-273.15, 50.0)


@pytest.mark.asyncio
async def test_live_sensor_fusion_radiation_bias_and_humidity_sentinel_fallback(
    tmp_path: Path,
) -> None:
    """Verify eTRV radiation bias correction, weighted sensor fusion, and 255% sentinel fallback."""
    store = ThermalStore(tmp_path / "thermal_live.db")
    await store.initialize()
    await store.upsert_zone_model(
        ThermalZoneModelRecord(
            zone_id="living_room",
            volume_m3=72.8,
            k_loss=0.06,
            etrv_bias_factor=2.5,
            static_offset_c=0.5,
        )
    )

    topology = _build_synthetic_topology()
    engine = ThermalPhysicsEngine(topology=topology, store=store, settings=ThermalSettings())

    now = datetime(2026, 11, 15, 22, 0, tzinfo=UTC)
    raw_states: list[dict[str, Any]] = [
        {"entity_id": "sensor.outdoor_temperature", "state": "4.0", "attributes": {}},
        {"entity_id": "weather.home", "state": "cloudy", "attributes": {"temperature": 4.0, "humidity": 80}},
        # Living room: eTRV reads 22.0 with 60% valve, bias=2.5, offset=0.5 -> corrected=20.0
        # Ambient sensor reads 20.0 -> fused true_temp_c=20.0, spatial_gradient_c=2.0
        {
            "entity_id": "climate.living_room_trv",
            "state": "heat",
            "attributes": {"current_temperature": 22.0, "temperature": 21.0, "hvac_action": "heating"},
        },
        {"entity_id": "sensor.living_room_valve", "state": "60.0", "attributes": {}},
        {"entity_id": "sensor.living_room_ambient_temp", "state": "20.0", "attributes": {}},
        {"entity_id": "sensor.living_room_humidity", "state": "50.0", "attributes": {}},
        {"entity_id": "binary_sensor.living_room_window", "state": "off", "attributes": {}},
        # Bedroom: humidity reports Bosch sentinel 255.0 -> must fall back to building median (50.0)
        {
            "entity_id": "climate.bedroom_trv",
            "state": "heat",
            "attributes": {"current_temperature": 19.5, "temperature": 19.0, "hvac_action": "idle"},
        },
        {"entity_id": "sensor.bedroom_valve", "state": "0.0", "attributes": {}},
        {"entity_id": "sensor.bedroom_ambient_temp", "state": "19.0", "attributes": {}},
        {"entity_id": "sensor.bedroom_humidity", "state": "255.0", "attributes": {}},
        {"entity_id": "binary_sensor.bedroom_window", "state": "off", "attributes": {}},
        # Study: valid humidity 50.0 and valve open while hvac_action='off' -> live anomaly
        {
            "entity_id": "climate.study_trv",
            "state": "off",
            "attributes": {"current_temperature": 20.0, "temperature": 21.0, "hvac_action": "off"},
        },
        {"entity_id": "sensor.study_valve", "state": "85.0", "attributes": {}},
        {"entity_id": "sensor.study_ambient_temp", "state": "19.0", "attributes": {}},
        {"entity_id": "sensor.study_humidity", "state": "50.0", "attributes": {}},
        {"entity_id": "binary_sensor.study_window", "state": "off", "attributes": {}},
    ]

    snap = await engine.update_live_states(raw_states, now_utc=now)
    assert snap.monitored_zone_count == 3
    assert snap.outdoor_temp_c == pytest.approx(4.0)
    assert snap.building_ref_humidity_pct == pytest.approx(50.0)

    liv = snap.zones["living_room"]
    assert liv.raw_etrv_temp_c == pytest.approx(22.0)
    assert liv.etrv_corrected_temp_c == pytest.approx(20.0)
    assert liv.ambient_temp_c == pytest.approx(20.0)
    assert liv.true_temp_c == pytest.approx(20.0)
    assert liv.spatial_gradient_c == pytest.approx(2.0)
    assert liv.dew_point_c == pytest.approx(9.27, abs=0.05)
    assert liv.absolute_humidity_g_m3 == pytest.approx(8.65, abs=0.05)
    assert liv.anomaly_detected is False

    bed = snap.zones["bedroom"]
    assert bed.rel_humidity_pct == pytest.approx(50.0)
    assert bed.humidity_source == "building_fallback"
    assert bed.dew_point_c is not None

    study = snap.zones["study"]
    assert study.anomaly_detected is True
    assert study.anomaly_reason == "valve_open_while_hvac_off"
    assert "study" in snap.anomaly_zones


@pytest.mark.asyncio
async def test_calibration_night_cooling_fit_and_ventilation_shock(tmp_path: Path) -> None:
    """Verify static/dynamic EMA calibration, 00:00-06:00 RC fit, and ventilation shock/rebound."""
    store = ThermalStore(tmp_path / "thermal_history.db")
    await store.initialize()

    topology = BuildingTopology(
        weather_entity="weather.home",
        outdoor_temp_sensors=["sensor.outdoor_temperature"],
        zones={"bedroom": _build_synthetic_topology().zones["bedroom"]},
    )
    engine = ThermalPhysicsEngine(topology=topology, store=store, settings=ThermalSettings())

    # Build 24h synthetic history starting 2026-11-15 00:00 UTC
    t0 = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
    k_true = 0.08
    t_start = 21.0
    t_out = 5.0

    ambient_hist: list[dict[str, Any]] = []
    etrv_hist: list[dict[str, Any]] = []
    valve_hist: list[dict[str, Any]] = []
    hum_hist: list[dict[str, Any]] = []
    out_temp_hist: list[dict[str, Any]] = []
    out_weather_hist: list[dict[str, Any]] = []
    contact_hist: list[dict[str, Any]] = []

    # 1. Night cooling window: 00:00 -> 06:00 (every 15 min)
    for step in range(25):
        ts = t0 + timedelta(minutes=15 * step)
        hours = (ts - t0).total_seconds() / 3600.0
        temp_exact = t_out + (t_start - t_out) * math.exp(-k_true * hours)
        ambient_hist.append({"last_updated": _iso(ts), "state": f"{temp_exact:.4f}"})
        # Static offset = +0.5 C while idle (valve == 0)
        etrv_hist.append(
            {
                "last_updated": _iso(ts),
                "state": "heat",
                "attributes": {
                    "current_temperature": round(temp_exact + 0.5, 4),
                    "temperature": 17.0,
                    "hvac_action": "idle",
                },
            }
        )
        valve_hist.append({"last_updated": _iso(ts), "state": "0.0"})
        hum_hist.append({"last_updated": _iso(ts), "state": "55.0"})
        out_temp_hist.append({"last_updated": _iso(ts), "state": f"{t_out:.2f}"})
        out_weather_hist.append(
            {
                "last_updated": _iso(ts),
                "state": "cloudy",
                "attributes": {"temperature": t_out, "humidity": 75.0},
            }
        )

    contact_hist.append({"last_updated": _iso(t0), "state": "off"})

    # 2. Ventilation shock episode: 08:00 -> 08:15 (rapid RH/temp drop, then rebound by 08:30)
    v_open = t0 + timedelta(hours=8)
    v_close = v_open + timedelta(minutes=15)
    contact_hist.append({"last_updated": _iso(v_open), "state": "on"})
    contact_hist.append({"last_updated": _iso(v_close), "state": "off"})

    vent_points = [
        (v_open, 20.5, 65.0),
        (v_open + timedelta(minutes=5), 18.8, 52.0),
        (v_open + timedelta(minutes=10), 17.9, 48.5),
        (v_close, 17.6, 48.0),
        (v_close + timedelta(minutes=15), 18.9, 49.0),
        (v_close + timedelta(minutes=25), 19.2, 49.5),
    ]
    for ts, temp_c, rh_pct in vent_points:
        ambient_hist.append({"last_updated": _iso(ts), "state": f"{temp_c:.3f}"})
        hum_hist.append({"last_updated": _iso(ts), "state": f"{rh_pct:.2f}"})
        etrv_hist.append(
            {
                "last_updated": _iso(ts),
                "state": "heat",
                "attributes": {"current_temperature": temp_c + 0.5, "temperature": 7.0, "hvac_action": "idle"},
            }
        )
        valve_hist.append({"last_updated": _iso(ts), "state": "0.0"})

    # 3. Active heating dynamic bias episode: 10:00 -> 11:00 (valve=80%, true etrv_bias ~ 3.5)
    h_start = t0 + timedelta(hours=10)
    for step in range(5):
        ts = h_start + timedelta(minutes=15 * step)
        amb = 19.2 + 0.35 * step
        # eTRV reads ambient + static_offset(~0.5) + 3.5 * 0.80
        etrv_temp = amb + 0.5 + 2.8
        ambient_hist.append({"last_updated": _iso(ts), "state": f"{amb:.3f}"})
        etrv_hist.append(
            {
                "last_updated": _iso(ts),
                "state": "heat",
                "attributes": {"current_temperature": round(etrv_temp, 3), "temperature": 22.0, "hvac_action": "heating"},
            }
        )
        valve_hist.append({"last_updated": _iso(ts), "state": "80.0"})
        hum_hist.append({"last_updated": _iso(ts), "state": "48.0"})

    history = {
        "sensor.bedroom_ambient_temp": ambient_hist,
        "climate.bedroom_trv": etrv_hist,
        "sensor.bedroom_valve": valve_hist,
        "sensor.bedroom_humidity": hum_hist,
        "binary_sensor.bedroom_window": contact_hist,
        "sensor.outdoor_temperature": out_temp_hist,
        "weather.home": out_weather_hist,
    }

    analysis = await engine.analyze_history(history, now_utc=t0 + timedelta(hours=12))

    # Calibration checks + SQLite persistence
    cal = analysis["calibrations"]["bedroom"]
    assert cal["static_updates"] >= 1
    assert cal["dynamic_updates"] >= 1
    assert cal["static_offset_c"] > 0.25
    assert cal["etrv_bias_factor"] > 2.5

    stored_model = await store.get_zone_model("bedroom")
    assert stored_model is not None
    assert stored_model.static_offset_c == pytest.approx(cal["static_offset_c"])
    assert stored_model.etrv_bias_factor == pytest.approx(cal["etrv_bias_factor"])

    # Nighttime cooling fit checks
    nc = analysis["night_cooling"]["bedroom"]
    assert nc["k_loss"] == pytest.approx(0.08, abs=0.005)
    assert nc["tau_hours"] == pytest.approx(12.5, abs=0.8)
    assert nc["cooling_rate_c_per_h"] > 0.5
    assert nc["r_squared"] > 0.99
    assert stored_model.k_loss == pytest.approx(0.08, abs=0.005)

    # Ventilation shock checks
    v_events = analysis["ventilation_events"]
    assert len(v_events) == 1
    ve = v_events[0]
    assert ve["zone_id"] == "bedroom"
    assert ve["indoor_abs_humidity_drop"] > 3.0
    assert ve["enthalpy_loss_kj"] > 100.0
    assert ve["rebound_temp_c"] == pytest.approx(19.2, abs=0.05)
    assert ve["rebound_delta_c"] == pytest.approx(1.6, abs=0.05)
    assert ve["knee_point_min"] is not None and 0.0 < ve["knee_point_min"] <= 15.0

    # SQLite observation rows persisted idempotently
    night_obs = await store.list_observations(zone_id="bedroom", event_type="night_cooling")
    vent_obs = await store.list_observations(zone_id="bedroom", event_type="ventilation_shock")
    assert len(night_obs) == 1
    assert len(vent_obs) == 1
    assert vent_obs[0].rebound_temp_c == pytest.approx(19.2, abs=0.05)


@pytest.mark.asyncio
async def test_simulation_hydraulic_balance_frost_protection_and_stall_anomaly(
    tmp_path: Path,
) -> None:
    """Verify scenario simulation, fast/slow hydraulic flagging, frost guard, and heating stall."""
    store = ThermalStore(tmp_path / "thermal_sim.db")
    await store.initialize()

    topology = _build_synthetic_topology()
    engine = ThermalPhysicsEngine(topology=topology, store=store, settings=ThermalSettings())

    # 1. Analyze synthetic heating rates + frost protection + stall anomaly
    t0 = datetime(2026, 11, 15, 6, 0, tzinfo=UTC)
    history: dict[str, list[dict[str, Any]]] = {
        "sensor.outdoor_temperature": [{"last_updated": _iso(t0), "state": "3.0"}],
    }

    # Study: fast_heating (+3.0 C/h over 1h)
    history["sensor.study_ambient_temp"] = [
        {"last_updated": _iso(t0 + timedelta(minutes=15 * i)), "state": f"{18.0 + 0.75 * i:.2f}"}
        for i in range(5)
    ]
    history["climate.study_trv"] = [
        {
            "last_updated": _iso(t0),
            "state": "heat",
            "attributes": {"current_temperature": 19.0, "temperature": 22.0, "hvac_action": "heating"},
        }
    ]
    history["sensor.study_valve"] = [{"last_updated": _iso(t0), "state": "90.0"}]
    history["binary_sensor.study_window"] = [{"last_updated": _iso(t0), "state": "off"}]

    # Living room: balanced (+1.5 C/h over 1h)
    history["sensor.living_room_ambient_temp"] = [
        {"last_updated": _iso(t0 + timedelta(minutes=15 * i)), "state": f"{18.0 + 0.375 * i:.3f}"}
        for i in range(5)
    ]
    history["climate.living_room_trv"] = [
        {
            "last_updated": _iso(t0),
            "state": "heat",
            "attributes": {"current_temperature": 19.0, "temperature": 21.0, "hvac_action": "heating"},
        }
    ]
    history["sensor.living_room_valve"] = [{"last_updated": _iso(t0), "state": "75.0"}]
    history["binary_sensor.living_room_window"] = [{"last_updated": _iso(t0), "state": "off"}]

    # Bedroom: slow_heating (+0.6 C/h over 1h)
    history["sensor.bedroom_ambient_temp"] = [
        {"last_updated": _iso(t0 + timedelta(minutes=15 * i)), "state": f"{18.0 + 0.15 * i:.2f}"}
        for i in range(5)
    ]
    history["climate.bedroom_trv"] = [
        {
            "last_updated": _iso(t0),
            "state": "heat",
            "attributes": {"current_temperature": 18.5, "temperature": 21.0, "hvac_action": "heating"},
        }
    ]
    history["sensor.bedroom_valve"] = [{"last_updated": _iso(t0), "state": "95.0"}]
    history["binary_sensor.bedroom_window"] = [{"last_updated": _iso(t0), "state": "off"}]

    bal_analysis = await engine.analyze_history(history, now_utc=t0 + timedelta(hours=2))
    assert bal_analysis["anomalies"] == []
    bal_by_zone = {row["zone_id"]: row for row in bal_analysis["hydraulic_balance"]}
    assert bal_by_zone["study"]["classification"] == "fast_heating"
    assert bal_by_zone["study"]["recommended_max_valve_pct"] is not None
    assert bal_by_zone["living_room"]["classification"] == "balanced"
    assert bal_by_zone["bedroom"]["classification"] == "slow_heating"

    # 2. Scenario simulation: 8h overnight projection + warm-up + ventilation recommendation
    live_snap = await engine.update_live_states(
        [
            {"entity_id": "sensor.outdoor_temperature", "state": "2.0", "attributes": {}},
            {"entity_id": "weather.home", "state": "clear", "attributes": {"temperature": 2.0, "humidity": 75}},
            {"entity_id": "sensor.bedroom_ambient_temp", "state": "20.0", "attributes": {}},
            {"entity_id": "sensor.bedroom_humidity", "state": "62.0", "attributes": {}},
            {
                "entity_id": "climate.bedroom_trv",
                "state": "heat",
                "attributes": {"current_temperature": 20.0, "temperature": 21.0, "hvac_action": "idle"},
            },
            {"entity_id": "sensor.bedroom_valve", "state": "0.0", "attributes": {}},
            {"entity_id": "binary_sensor.bedroom_window", "state": "off", "attributes": {}},
        ],
        now_utc=t0 + timedelta(hours=2),
    )
    sim = await engine.simulate_scenario(
        live_snapshot=live_snap,
        zone_id="bedroom",
        outdoor_temp_curve_c=[2.0, 1.5, 1.0, 0.5, 0.0, 0.0, 0.5, 1.0],
        hours_ahead=8.0,
        target_setpoint_c=21.0,
    )
    bed_proj = sim["projections"]["bedroom"]
    assert len(bed_proj["hourly_temps_c"]) == 8
    assert bed_proj["projected_min_temp_c"] < 20.0
    assert bed_proj["estimated_warmup_min"] > 0.0
    assert 3.0 <= bed_proj["recommended_ventilation_min"] <= 25.0

    # 3. Frost protection (target <= 8.0C) must NOT flag heater malfunction;
    #    closed window + valve >= 80% + zero temp rise over >=45 min MUST flag heating_stall!
    t_anom = datetime(2026, 11, 15, 14, 0, tzinfo=UTC)
    anomaly_hist: dict[str, list[dict[str, Any]]] = {
        "sensor.outdoor_temperature": [{"last_updated": _iso(t_anom), "state": "2.0"}],
        # Bedroom: frost protection target=7.0C, valve=90%, flat temp -> NOT an anomaly
        "sensor.bedroom_ambient_temp": [
            {"last_updated": _iso(t_anom + timedelta(minutes=15 * i)), "state": "18.0"}
            for i in range(5)
        ],
        "climate.bedroom_trv": [
            {
                "last_updated": _iso(t_anom),
                "state": "heat",
                "attributes": {"current_temperature": 18.0, "temperature": 7.0, "hvac_action": "idle"},
            }
        ],
        "sensor.bedroom_valve": [{"last_updated": _iso(t_anom), "state": "90.0"}],
        "binary_sensor.bedroom_window": [{"last_updated": _iso(t_anom), "state": "off"}],
        # Study: closed window, target=22.0C, valve=92%, flat temp for 60 min -> heating_stall!
        "sensor.study_ambient_temp": [
            {"last_updated": _iso(t_anom + timedelta(minutes=15 * i)), "state": "18.0"}
            for i in range(5)
        ],
        "climate.study_trv": [
            {
                "last_updated": _iso(t_anom),
                "state": "heat",
                "attributes": {"current_temperature": 18.0, "temperature": 22.0, "hvac_action": "heating"},
            }
        ],
        "sensor.study_valve": [{"last_updated": _iso(t_anom), "state": "92.0"}],
        "binary_sensor.study_window": [{"last_updated": _iso(t_anom), "state": "off"}],
    }

    anom_res = await engine.analyze_history(anomaly_hist, now_utc=t_anom + timedelta(hours=2))
    flagged_zones = {a["zone_id"]: a for a in anom_res["anomalies"]}
    assert "bedroom" not in flagged_zones
    assert "study" in flagged_zones
    assert flagged_zones["study"]["type"] == "heating_stall"

    # Also verify live frost protection suppresses valve_open_while_hvac_off
    frost_snap = await engine.update_live_states(
        [
            {
                "entity_id": "climate.bedroom_trv",
                "state": "heat",
                "attributes": {"current_temperature": 18.0, "temperature": 7.0, "hvac_action": "off"},
            },
            {"entity_id": "sensor.bedroom_valve", "state": "90.0", "attributes": {}},
            {"entity_id": "binary_sensor.bedroom_window", "state": "off", "attributes": {}},
        ],
        now_utc=t_anom,
    )
    assert frost_snap.zones["bedroom"].frost_protection_active is True
    assert frost_snap.zones["bedroom"].anomaly_detected is False
