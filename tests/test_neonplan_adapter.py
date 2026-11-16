"""Unit tests for the NeonPlan 3D topology, geometry, entity binding, and override adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.core.entity_resolver import RegistryEntityRecord
from src.core.neonplan_adapter import (
    NeonPlanAdapter,
    canonical_zone_id,
    compute_polygon_area,
)
from src.core.text_tr import normalize_tr, stem_tr_phrase


def _make_record(
    entity_id: str,
    friendly_name: str,
    area_id: str | None = None,
    area_name: str | None = None,
    disabled: bool = False,
) -> RegistryEntityRecord:
    domain = entity_id.split(".", 1)[0]
    combined = normalize_tr(f"{friendly_name} {area_name or ''} {entity_id.split('.', 1)[-1]}")
    _, stems = stem_tr_phrase(combined, strip_stopwords=False)
    _, area_stems = stem_tr_phrase(area_name or "", strip_stopwords=False)
    return RegistryEntityRecord(
        entity_id=entity_id,
        domain=domain,
        friendly_name=friendly_name,
        area_id=area_id,
        area_name=area_name,
        disabled=disabled,
        search_text_norm=combined,
        search_stems=stems,
        area_stems=area_stems,
    )


def test_shoelace_l_polygon_area_wall_adjacency_and_overrides(tmp_path: Path) -> None:
    """Verify non-convex L-polygon area, volume, shared/external walls, and YAML overrides."""
    # L-shaped living room: 6x6 square minus 3x3 top-right corner = 27.0 m²
    l_points = [
        [0.0, 0.0],
        [6.0, 0.0],
        [6.0, 3.0],
        [3.0, 3.0],
        [3.0, 6.0],
        [0.0, 6.0],
    ]
    assert compute_polygon_area(l_points) == pytest.approx(27.0)

    floorplan: dict[str, Any] = {
        "building": {
            "version": 1,
            "weather_entity": "weather.home",
            "outdoor_temp_sensors": ["sensor.outdoor_temperature"],
            "floors": [
                {
                    "id": "ground",
                    "name": "Ground Floor",
                    "height": 2.6,
                    "rooms": [
                        {
                            "id": "room_living",
                            "name": "Living Room",
                            "area_id": "living_room",
                            "points": l_points,
                            "climate": "climate.living_room_trv",
                        },
                        {
                            "id": "room_study",
                            "name": "Study",
                            "area_id": "study",
                            # Shares x=6.0 wall from z=0..3 (length 3.0 m) with Living Room
                            "points": [
                                [6.0, 0.0],
                                [9.0, 0.0],
                                [9.0, 3.0],
                                [6.0, 3.0],
                            ],
                            "climate": "climate.study_trv",
                        },
                        {
                            "id": "room_shed",
                            "name": "Detached Shed",
                            "area_id": "shed",
                            # Disjoint polygon -> zero shared walls, 4 external walls
                            "points": [
                                [15.0, 15.0],
                                [17.0, 15.0],
                                [17.0, 17.0],
                                [15.0, 17.0],
                            ],
                        },
                    ],
                }
            ],
        }
    }
    plan_path = tmp_path / "floorplan.json"
    plan_path.write_text(json.dumps(floorplan), encoding="utf-8")

    overrides_path = tmp_path / "thermal_overrides.yaml"
    overrides_path.write_text(
        """zones:
  living_room:
    ceiling_height_m: 3.0
    insulation_factor: 0.9
    exterior_exposure: "south_west"
    extra_climate_entities:
      - "climate.living_room_aux_heater"
    extra_temperature_sensors:
      - "sensor.living_room_ambient_temp"
    extra_humidity_sensors:
      - "sensor.living_room_humidity"
    extra_contact_sensors:
      - "binary_sensor.living_room_patio_contact"
    extra_valve_sensors:
      - "sensor.living_room_valve_pos"
    max_valve_entity: "number.living_room_max_valve"
  detached_shed:
    exclude: true
""",
        encoding="utf-8",
    )

    adapter = NeonPlanAdapter()
    topology = adapter.load(plan_path, overrides_path=overrides_path)

    assert topology.weather_entity == "weather.home"
    assert topology.outdoor_temp_sensors == ["sensor.outdoor_temperature"]

    living = topology.zones["living_room"]
    assert living.floor_area_m2 == pytest.approx(27.0)
    assert living.ceiling_height_m == pytest.approx(3.0)
    assert living.volume_m3 == pytest.approx(81.0)
    assert living.insulation_factor == pytest.approx(0.9)
    assert living.exterior_exposure == "south_west"
    assert "study" in living.adjacent_zones
    assert living.shared_wall_lengths_m["study"] == pytest.approx(3.0)
    assert living.external_walls == 5  # 6 edges total, 1 fully shared edge of length 3.0
    assert "climate.living_room_aux_heater" in living.climate_entities
    assert "sensor.living_room_humidity" in living.humidity_sensors
    assert "binary_sensor.living_room_patio_contact" in living.contact_sensors
    assert "sensor.living_room_valve_pos" in living.valve_position_sensors
    assert living.max_valve_entity == "number.living_room_max_valve"
    assert any(s.entity_id == "sensor.living_room_ambient_temp" for s in living.temperature_sensors)

    study = topology.zones["study"]
    assert study.floor_area_m2 == pytest.approx(9.0)
    assert study.ceiling_height_m == pytest.approx(2.6)
    assert study.volume_m3 == pytest.approx(23.4)
    assert study.adjacent_zones == ["living_room"]
    assert study.shared_wall_lengths_m == {"living_room": pytest.approx(3.0)}
    assert study.external_walls == 3

    shed = topology.zones["detached_shed"]
    assert shed.adjacent_zones == []
    assert shed.shared_wall_lengths_m == {}
    assert shed.external_walls == 4
    assert shed.excluded is True

    active_ids = {z.zone_id for z in topology.active_zones()}
    assert active_ids == {"living_room", "study"}
    monitored_ids = {z.zone_id for z in topology.monitored_zones()}
    assert monitored_ids == {"living_room", "study"}


def test_entity_binding_spatial_classification_and_registry_tie_breaking(tmp_path: Path) -> None:
    """Verify furniture/openings/placements binding, <0.5m vs >=0.5m classification, and area tie-break."""
    floorplan: dict[str, Any] = {
        "building": {
            "version": 1,
            "floors": [
                {
                    "id": "floor_1",
                    "name": "Upper Floor",
                    "height": 2.5,
                    "rooms": [
                        {
                            "id": "room_bedroom",
                            "name": "Bedroom",
                            "area_id": "bedroom",
                            "points": [[0.0, 0.0], [5.0, 0.0], [5.0, 4.0], [0.0, 4.0]],
                            "climate": {
                                "temperature": "sensor.bedroom_wall_temp",
                                "humidity": "sensor.bedroom_wall_humidity",
                            },
                        },
                        {
                            "id": "room_attic_main",
                            "name": "Attic Loft",
                            "area_id": "upper_loft",
                            "points": [[5.0, 0.0], [10.0, 0.0], [10.0, 5.0], [5.0, 5.0]],
                        },
                        {
                            "id": "room_attic_closet",
                            "name": "Attic Closet",
                            "area_id": "upper_loft",
                            "points": [[5.0, 5.0], [7.0, 5.0], [7.0, 7.0], [5.0, 7.0]],
                        },
                    ],
                    "openings": [
                        {
                            "id": "win_bed_1",
                            "room_id": "room_bedroom",
                            "type": "window",
                            "width": 2.0,
                            "height": 1.5,
                            "contact": "binary_sensor.bedroom_window_contact",
                            "cover": "cover.bedroom_blind",
                        },
                        {
                            "id": "door_bed_1",
                            "room_id": "room_bedroom",
                            "type": "door",
                            "width": 0.9,
                            "height": 2.1,
                            "contact": "binary_sensor.bedroom_door_contact",
                        },
                    ],
                    "furniture": [
                        {
                            "id": "rad_bed",
                            "type": "radiator",
                            "x": 2.5,
                            "z": 0.15,
                            "entity": "climate.bedroom_trv",
                            "valve_entity": "sensor.bedroom_trv_valve",
                        },
                        {
                            "id": "ac_bed",
                            "type": "ac_unit",
                            "x": 0.2,
                            "z": 3.5,
                            "entity": "climate.bedroom_ac",
                            "temperature_entity": "sensor.bedroom_ac_climatecontrol_room_temperature",
                        },
                        {
                            "id": "wall_sw_attic",
                            "type": "wall_switch",
                            "x": 6.0,
                            "z": 2.0,
                            "entity": "sensor.attic_wall_switch_temp",
                        },
                    ],
                    "placements": [
                        {
                            "entity_id": "sensor.bedroom_near_rad_temp",
                            "x": 2.6,
                            "z": 0.35,  # ~0.224m from radiator (2.5, 0.15) -> on_radiator
                            "y": 0.6,
                        },
                        {
                            "entity_id": "sensor.bedroom_wall_temp",
                            "x": 2.5,
                            "z": 3.5,  # 3.35m from radiator -> ambient_room
                            "y": 1.5,
                        },
                        {
                            "entity_id": "sensor.bedroom_boundary_temp",
                            "x": 2.5,
                            "z": 0.65,  # Exactly 0.50m -> >= 0.5m -> ambient_room
                            "y": 1.0,
                        },
                        {
                            "entity_id": "cover.bedroom_blind",
                            "x": 2.5,
                            "z": 0.1,
                            "y": 2.0,
                        },
                        {
                            "entity_id": "camera.bedroom_entry",
                            "x": 0.5,
                            "z": 0.5,
                            "y": 2.1,
                        },
                    ],
                }
            ],
        }
    }

    records = [
        _make_record(
            "binary_sensor.bedroom_extra_window",
            "Bedroom Extra Window Contact",
            area_id="bedroom",
            area_name="Bedroom",
        ),
        _make_record(
            "number.bedroom_max_valve_position",
            "Bedroom Max Valve Position",
            area_id="bedroom",
            area_name="Bedroom",
        ),
        # Noise entities must be ignored
        _make_record(
            "sensor.bedroom_trv_battery",
            "Bedroom TRV Battery",
            area_id="bedroom",
            area_name="Bedroom",
        ),
        _make_record(
            "sensor.bedroom_disabled_temp",
            "Bedroom Disabled Temperature",
            area_id="bedroom",
            area_name="Bedroom",
            disabled=True,
        ),
        # Multi-room shared area_id="upper_loft" tie-breaking:
        # 1) Token overlap picks Attic Closet
        _make_record(
            "sensor.attic_closet_temperature",
            "Attic Closet Temperature",
            area_id="upper_loft",
            area_name="Upper Loft",
        ),
        # 2) Zero token overlap falls back to largest floor_area_m2 (Attic Loft = 25 m² > 4 m²)
        _make_record(
            "sensor.upper_loft_humidity",
            "Upper Loft Humidity",
            area_id="upper_loft",
            area_name="Upper Loft",
        ),
    ]

    plan_path = tmp_path / "sensors_plan.json"
    plan_path.write_text(json.dumps(floorplan), encoding="utf-8")

    topology = NeonPlanAdapter(
        radiator_proximity_threshold_m=0.50,
        default_etrv_bias_factor=2.5,
    ).load(plan_path, registry_records=records)

    bedroom = topology.zones["bedroom"]
    assert bedroom.window_area_m2 == pytest.approx(3.0)  # Only window (2.0*1.5), not door
    assert set(bedroom.contact_sensors) == {
        "binary_sensor.bedroom_window_contact",
        "binary_sensor.bedroom_door_contact",
        "binary_sensor.bedroom_extra_window",
    }
    assert set(bedroom.climate_entities) == {"climate.bedroom_trv", "climate.bedroom_ac"}
    assert "sensor.bedroom_trv_valve" in bedroom.valve_position_sensors
    assert "sensor.bedroom_wall_humidity" in bedroom.humidity_sensors
    assert bedroom.max_valve_entity == "number.bedroom_max_valve_position"

    sensors_by_id = {s.entity_id: s for s in bedroom.temperature_sensors}
    assert "sensor.bedroom_trv_battery" not in sensors_by_id
    assert "sensor.bedroom_disabled_temp" not in sensors_by_id
    assert "cover.bedroom_blind" not in sensors_by_id
    assert "camera.bedroom_entry" not in sensors_by_id

    near_rad = sensors_by_id["sensor.bedroom_near_rad_temp"]
    assert near_rad.placement == "on_radiator"
    assert near_rad.radiation_bias_factor == pytest.approx(2.5)
    assert near_rad.distance_to_radiator_m is not None and near_rad.distance_to_radiator_m < 0.50

    etrv_virtual = sensors_by_id["climate.bedroom_trv"]
    assert etrv_virtual.placement == "on_radiator"
    assert etrv_virtual.radiation_bias_factor == pytest.approx(2.5)

    wall_ambient = sensors_by_id["sensor.bedroom_wall_temp"]
    assert wall_ambient.placement == "ambient_room"
    assert wall_ambient.radiation_bias_factor == pytest.approx(0.0)
    assert wall_ambient.reliability_weight == pytest.approx(1.0)

    boundary_ambient = sensors_by_id["sensor.bedroom_boundary_temp"]
    assert boundary_ambient.placement == "ambient_room"
    assert boundary_ambient.radiation_bias_factor == pytest.approx(0.0)

    assert "climate.bedroom_ac" not in sensors_by_id
    ac_temp_sensor = sensors_by_id["sensor.bedroom_ac_climatecontrol_room_temperature"]
    assert ac_temp_sensor.placement == "ambient_room"
    assert ac_temp_sensor.radiation_bias_factor == pytest.approx(0.0)
    assert ac_temp_sensor.reliability_weight == pytest.approx(0.3)

    # Multi-room shared area_id enrichment verification
    attic_closet = topology.zones["attic_closet"]
    attic_loft = topology.zones["attic_loft"]
    assert any(s.entity_id == "sensor.attic_wall_switch_temp" and s.placement == "ambient_room" for s in attic_loft.temperature_sensors)
    assert any(s.entity_id == "sensor.attic_closet_temperature" for s in attic_closet.temperature_sensors)
    assert "sensor.upper_loft_humidity" in attic_loft.humidity_sensors
    assert "sensor.upper_loft_humidity" not in attic_closet.humidity_sensors


def test_schema_variation_fallbacks_and_zone_resolution(tmp_path: Path) -> None:
    """Verify tolerance for top-level rooms, missing polygons, dict placements, and TR slugs."""
    assert canonical_zone_id("Çalışma Odası") == "calisma_odasi"
    assert canonical_zone_id("Giriş Holü") == "giris_holu"
    seen = {"study"}
    assert canonical_zone_id("Study", existing_ids=seen) == "study_2"

    # Minimal schema: no 'building' wrapper, top-level 'rooms', bbox coords, dict placements
    minimal_payload: dict[str, Any] = {
        "rooms": [
            {
                "id": "r_study",
                "name": "Çalışma Odası",
                "area_id": "study_area",
                "x": 0.0,
                "z": 0.0,
                "width": 4.0,
                "depth": 3.0,
                "climate": "climate.study_trv",
            },
            {
                "id": "r_hall",
                "name": "Hallway",
                "floor_area_m2": 6.5,
                "climate": {
                    "thermostat": ["climate.hall_trv"],
                    "temperature": ["sensor.hall_ambient"],
                    "humidity": "sensor.hall_humidity",
                    "valve": "sensor.hall_valve",
                    "contact": "binary_sensor.hall_door",
                },
            },
        ],
        "placements": {
            "sensor.study_desk_temp": {"room_id": "r_study", "x": 2.0, "z": 1.5},
        },
    }

    plan_path = tmp_path / "minimal_plan.json"
    plan_path.write_text(json.dumps(minimal_payload), encoding="utf-8")

    topology = NeonPlanAdapter(default_ceiling_height_m=2.70).load(plan_path)
    study = topology.zones["calisma_odasi"]
    assert study.floor_area_m2 == pytest.approx(12.0)
    assert study.ceiling_height_m == pytest.approx(2.70)
    assert study.volume_m3 == pytest.approx(32.4)
    assert study.climate_entities == ["climate.study_trv"]
    assert any(s.entity_id == "sensor.study_desk_temp" for s in study.temperature_sensors)

    hall = topology.zones["hallway"]
    assert hall.floor_area_m2 == pytest.approx(6.5)
    assert hall.climate_entities == ["climate.hall_trv"]
    assert "sensor.hall_humidity" in hall.humidity_sensors
    assert "sensor.hall_valve" in hall.valve_position_sensors
    assert "binary_sensor.hall_door" in hall.contact_sensors

    # Zone resolution across Turkish normalization, area_id, and partial names
    assert topology.resolve_zone("Çalışma Odası") is study
    assert topology.resolve_zone("calisma odasi") is study
    assert topology.resolve_zone("study_area") is study
    assert topology.resolve_zone("r_hall") is hall
