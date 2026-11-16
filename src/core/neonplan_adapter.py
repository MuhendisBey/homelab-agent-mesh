"""NeonPlan 3D floorplan JSON + HA registry + YAML override topology adapter."""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from src.core.entity_resolver import RegistryEntityRecord
from src.core.text_tr import normalize_tr

logger = logging.getLogger(__name__)

_NON_SNAKE_RE = re.compile(r"[^a-z0-9]+")
_WALL_ASSIGN_TOL_M = 0.35
_OPENING_ASSIGN_TOL_M = 0.50

_NOISE_ENTITY_SUBSTRINGS: tuple[str, ...] = (
    "battery",
    "voltage",
    "outdoor",
    "electrical_consumption",
    "oven",
    "collector",
    "dhw",
    "return_temperature",
    "supply_temperature",
    "pcb_temp",
    "chip_temp",
    "fault",
    "boot",
    "reboot",
    "uptime",
    "signal",
    "warning",
    "caution",
    "error_code",
    "thread_",
    "last_",
)

_CONTACT_KEYWORDS: tuple[str, ...] = (
    "window",
    "door",
    "contact",
    "pencere",
    "kapi",
    "balkon",
    "opening",
)

_AC_KEYWORDS: tuple[str, ...] = (
    "climatecontrol",
    "klima",
    "air_conditioner",
    "ac_unit",
)


# ---------------------------------------------------------------------------
# Pure geometry helpers
# ---------------------------------------------------------------------------


def canonical_zone_id(name: str, existing_ids: set[str] | None = None) -> str:
    """Convert a room/zone name to a unique ASCII snake_case zone identifier."""
    norm = normalize_tr(name)
    base = _NON_SNAKE_RE.sub("_", norm).strip("_") or "zone"
    if existing_ids is None or base not in existing_ids:
        return base
    idx = 2
    while f"{base}_{idx}" in existing_ids:
        idx += 1
    return f"{base}_{idx}"


def compute_polygon_area(points: Sequence[Sequence[float]]) -> float:
    """Compute 2D polygon area in m² via the Shoelace formula."""
    n = len(points)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x1, z1 = float(points[i][0]), float(points[i][1])
        x2, z2 = float(points[(i + 1) % n][0]), float(points[(i + 1) % n][1])
        total += x1 * z2 - x2 * z1
    return 0.5 * abs(total)


def _point_to_segment_distance(
    px: float,
    pz: float,
    x1: float,
    z1: float,
    x2: float,
    z2: float,
) -> float:
    dx = x2 - x1
    dz = z2 - z1
    len_sq = dx * dx + dz * dz
    if len_sq <= 1e-12:
        return math.hypot(px - x1, pz - z1)
    t = ((px - x1) * dx + (pz - z1) * dz) / len_sq
    t_clamped = max(0.0, min(1.0, t))
    proj_x = x1 + t_clamped * dx
    proj_z = z1 + t_clamped * dz
    return math.hypot(px - proj_x, pz - proj_z)


def distance_to_polygon_edges(x: float, z: float, polygon: Sequence[Sequence[float]]) -> float:
    """Return minimum Euclidean distance in meters from ``(x, z)`` to ``polygon`` edges."""
    n = len(polygon)
    if n == 0:
        return math.inf
    if n == 1:
        return math.hypot(x - float(polygon[0][0]), z - float(polygon[0][1]))
    min_dist = math.inf
    for i in range(n):
        x1, z1 = float(polygon[i][0]), float(polygon[i][1])
        x2, z2 = float(polygon[(i + 1) % n][0]), float(polygon[(i + 1) % n][1])
        dist = _point_to_segment_distance(x, z, x1, z1, x2, z2)
        min_dist = min(min_dist, dist)
    return min_dist


def point_in_polygon(x: float, z: float, polygon: Sequence[Sequence[float]]) -> bool:
    """Determine if ``(x, z)`` lies inside or on the boundary of ``polygon`` via ray-casting."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    for i in range(n):
        x1, z1 = float(polygon[i][0]), float(polygon[i][1])
        x2, z2 = float(polygon[(i + 1) % n][0]), float(polygon[(i + 1) % n][1])
        if _point_to_segment_distance(x, z, x1, z1, x2, z2) <= 1e-9:
            return True
        if ((z1 > z) != (z2 > z)) and (x < (x2 - x1) * (z - z1) / (z2 - z1) + x1):
            inside = not inside
    return inside


def segment_overlap_length(
    p1: Sequence[float],
    p2: Sequence[float],
    q1: Sequence[float],
    q2: Sequence[float],
    dist_tol: float = 0.25,
    min_overlap: float = 0.25,
) -> float:
    """Return shared length of two nearly-parallel wall segments, or ``0.0`` if not shared."""
    p1x, p1z = float(p1[0]), float(p1[1])
    p2x, p2z = float(p2[0]), float(p2[1])
    q1x, q1z = float(q1[0]), float(q1[1])
    q2x, q2z = float(q2[0]), float(q2[1])

    dx1, dz1 = p2x - p1x, p2z - p1z
    dx2, dz2 = q2x - q1x, q2z - q1z
    len1 = math.hypot(dx1, dz1)
    len2 = math.hypot(dx2, dz2)
    if len1 <= 1e-9 or len2 <= 1e-9:
        return 0.0

    u1x, u1z = dx1 / len1, dz1 / len1
    u2x, u2z = dx2 / len2, dz2 / len2
    dot = u1x * u2x + u1z * u2z
    if abs(dot) < 0.98:
        return 0.0

    s1 = (q1x - p1x) * u1x + (q1z - p1z) * u1z
    s2 = (q2x - p1x) * u1x + (q2z - p1z) * u1z
    q_min = min(s1, s2)
    q_max = max(s1, s2)
    ov_start = max(0.0, q_min)
    ov_end = min(len1, q_max)
    overlap = ov_end - ov_start
    if overlap < min_overlap:
        return 0.0

    s_mid = 0.5 * (ov_start + ov_end)
    p_mid_x = p1x + s_mid * u1x
    p_mid_z = p1z + s_mid * u1z
    denom = s2 - s1
    if abs(denom) <= 1e-9:
        return 0.0
    t_q = (s_mid - s1) / denom
    q_mid_x = q1x + t_q * dx2
    q_mid_z = q1z + t_q * dz2
    perp_dist = math.hypot(q_mid_x - p_mid_x, q_mid_z - p_mid_z)
    if perp_dist > dist_tol:
        return 0.0
    return float(overlap)


# ---------------------------------------------------------------------------
# Topology Pydantic models
# ---------------------------------------------------------------------------


class RadiatorPlacement(BaseModel):
    """Physical radiator object extracted from the 3D floorplan."""

    id: str
    x: float
    z: float
    width_m: float = 1.0
    height_m: float = 0.6
    climate_entity: str | None = None
    valve_sensor_entity: str | None = None
    furniture_type: str = "radiator"


class SensorPlacement(BaseModel):
    """Temperature/thermal sensor mapped to a zone with spatial bias classification."""

    entity_id: str
    sensor_type: Literal["temperature", "humidity", "valve_position", "contact"]
    x: float | None = None
    y: float | None = None
    z: float | None = None
    distance_to_radiator_m: float | None = None
    placement: Literal["on_radiator", "ambient_room", "unknown"] = "unknown"
    radiation_bias_factor: float = 0.0
    reliability_weight: float = 1.0
    source: str = "neonplan"


class OpeningSpec(BaseModel):
    """Window or door opening belonging to a zone."""

    id: str
    opening_type: Literal["window", "door"]
    width_m: float
    height_m: float
    area_m2: float
    contact_entities: list[str] = Field(default_factory=list)
    cover_entity: str | None = None
    shared_with_zone: str | None = None


class ZoneOverride(BaseModel):
    """Optional manual metadata and entity overrides from ``thermal_overrides.yaml``."""

    zone_id: str | None = None
    ceiling_height_m: float | None = None
    insulation_factor: float | None = None
    exterior_exposure: str | None = None
    exclude: bool = False
    extra_climate_entities: list[str] = Field(default_factory=list)
    extra_temperature_sensors: list[str] = Field(default_factory=list)
    extra_humidity_sensors: list[str] = Field(default_factory=list)
    extra_contact_sensors: list[str] = Field(default_factory=list)
    extra_valve_sensors: list[str] = Field(default_factory=list)
    exclude_entities: list[str] = Field(default_factory=list)
    max_valve_entity: str | None = None


class ZoneTopology(BaseModel):
    """Complete physical geometry and Home Assistant instrumentation for a single room."""

    zone_id: str
    room_id: str
    name: str
    floor_id: str
    floor_name: str
    area_id: str | None = None
    polygon: list[tuple[float, float]] = Field(default_factory=list)
    floor_area_m2: float
    ceiling_height_m: float
    volume_m3: float
    adjacent_zones: list[str] = Field(default_factory=list)
    shared_wall_lengths_m: dict[str, float] = Field(default_factory=dict)
    external_walls: int = 0
    external_wall_length_m: float = 0.0
    window_area_m2: float = 0.0
    openings: list[OpeningSpec] = Field(default_factory=list)
    radiators: list[RadiatorPlacement] = Field(default_factory=list)
    climate_entities: list[str] = Field(default_factory=list)
    temperature_sensors: list[SensorPlacement] = Field(default_factory=list)
    humidity_sensors: list[str] = Field(default_factory=list)
    valve_position_sensors: list[str] = Field(default_factory=list)
    contact_sensors: list[str] = Field(default_factory=list)
    max_valve_entity: str | None = None
    insulation_factor: float = 1.0
    exterior_exposure: str | None = None
    excluded: bool = False


class BuildingTopology(BaseModel):
    """Full parsed building digital twin topology across floors and zones."""

    version: int = 1
    weather_entity: str | None = None
    outdoor_temp_sensors: list[str] = Field(default_factory=list)
    north_azimuth_deg: float | None = None
    zones: dict[str, ZoneTopology] = Field(default_factory=dict)

    def active_zones(self) -> list[ZoneTopology]:
        """Return all non-excluded zones."""
        return [z for z in self.zones.values() if not z.excluded]

    def monitored_zones(self) -> list[ZoneTopology]:
        """Return active zones that have thermal instrumentation or radiators."""
        return [
            z
            for z in self.zones.values()
            if not z.excluded and bool(z.climate_entities or z.temperature_sensors or z.radiators)
        ]

    def resolve_zone(self, query: str) -> ZoneTopology | None:
        """Resolve a zone by exact ID, room_id, area_id, normalized name, or token overlap."""
        cleaned = query.strip()
        if not cleaned:
            return None
        if cleaned in self.zones:
            return self.zones[cleaned]
        q_lower = cleaned.lower()
        for zone in self.zones.values():
            if zone.zone_id.lower() == q_lower or zone.room_id.lower() == q_lower:
                return zone

        q_norm = normalize_tr(cleaned)
        if not q_norm:
            return None
        q_snake = _NON_SNAKE_RE.sub("_", q_norm).strip("_")

        for zone in self.zones.values():
            if zone.zone_id == q_snake:
                return zone
            if normalize_tr(zone.name) == q_norm:
                return zone
            if zone.area_id and normalize_tr(zone.area_id) == q_norm:
                return zone

        q_tokens = set(q_norm.split())
        best_score = 0.0
        best_zone: ZoneTopology | None = None
        for zone in self.zones.values():
            candidates = [
                normalize_tr(zone.name),
                normalize_tr(zone.zone_id.replace("_", " ")),
                normalize_tr(zone.area_id.replace("_", " ")) if zone.area_id else "",
            ]
            for cand in candidates:
                if not cand:
                    continue
                score = 0.0
                if cand.startswith(q_norm) or q_norm in cand or cand in q_norm:
                    overlap_ratio = min(len(q_norm), len(cand)) / max(len(q_norm), len(cand))
                    score = 0.70 + 0.25 * overlap_ratio
                else:
                    c_tokens = set(cand.split())
                    if q_tokens and c_tokens:
                        inter = len(q_tokens & c_tokens)
                        if inter > 0:
                            score = 0.60 * (inter / len(q_tokens | c_tokens)) + 0.25 * (
                                inter / len(q_tokens)
                            )
                if not zone.excluded:
                    score += 0.02
                if score > best_score and score >= 0.35:
                    best_score = score
                    best_zone = zone
        return best_zone


# ---------------------------------------------------------------------------
# Internal parsing helpers
# ---------------------------------------------------------------------------


def _as_float(val: Any, default: float | None = None) -> float | None:
    if val is None or isinstance(val, bool):
        return default
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    return num if math.isfinite(num) else default


def _as_str(val: Any) -> str | None:
    if isinstance(val, str):
        stripped = val.strip()
        return stripped or None
    return None


def _append_unique(items: list[str], value: str | None) -> None:
    if value and value not in items:
        items.append(value)


def _extract_coords(raw: Mapping[str, Any]) -> tuple[float | None, float | None, float | None]:
    pos = raw.get("position")
    src: Mapping[str, Any] = pos if isinstance(pos, Mapping) else raw
    x = _as_float(src.get("x"))
    y = _as_float(src.get("y"))
    z = _as_float(src.get("z"))
    if (
        x is not None
        and z is None
        and y is not None
        and not isinstance(pos, Mapping)
        and ("height" in raw or "h" in raw or "width" in raw or "w" in raw)
    ):
        # 2D dicts sometimes label the second horizontal coordinate as y instead of z.
        z = y
        y = None
    return x, y, z


def _extract_polygon(room: Mapping[str, Any]) -> list[tuple[float, float]]:
    raw_pts = (
        room.get("polygon")
        or room.get("points")
        or room.get("vertices")
        or room.get("corners")
        or room.get("shape")
    )
    pts: list[tuple[float, float]] = []
    if isinstance(raw_pts, Sequence) and not isinstance(raw_pts, (str, bytes)):
        for pt in raw_pts:
            if isinstance(pt, Mapping):
                px = _as_float(pt.get("x"))
                pz = _as_float(pt.get("z"))
                if pz is None:
                    pz = _as_float(pt.get("y"))
                if px is not None and pz is not None:
                    pts.append((px, pz))
            elif isinstance(pt, Sequence) and not isinstance(pt, (str, bytes)) and len(pt) >= 2:
                px = _as_float(pt[0])
                pz = _as_float(pt[2] if len(pt) >= 3 else pt[1])
                if px is not None and pz is not None:
                    pts.append((px, pz))
    if len(pts) >= 3:
        return pts

    rx = _as_float(room.get("x"))
    rz = _as_float(room.get("z"))
    rw = _as_float(room.get("width") or room.get("w") or room.get("width_m"))
    rd = _as_float(room.get("depth") or room.get("d") or room.get("length"))
    if rx is not None and rz is not None and rw and rd and rw > 0 and rd > 0:
        return [(rx, rz), (rx + rw, rz), (rx + rw, rz + rd), (rx, rz + rd)]
    return []


def _is_noise_entity(entity_id: str) -> bool:
    lowered = entity_id.lower()
    return any(tok in lowered for tok in _NOISE_ENTITY_SUBSTRINGS)


def _is_ac_climate(entity_id: str, known_ac_entities: set[str]) -> bool:
    if entity_id in known_ac_entities:
        return True
    lowered = entity_id.lower()
    if any(kw in lowered for kw in _AC_KEYWORDS):
        return True
    tokens = set(_NON_SNAKE_RE.split(normalize_tr(entity_id.split(".", 1)[-1])))
    return "ac" in tokens


def _is_ac_temp_entity(entity_id: str) -> bool:
    lowered = entity_id.lower()
    return "climatecontrol_room_temperature" in lowered or any(kw in lowered for kw in _AC_KEYWORDS)


def _is_etrv_entity(entity_id: str, known_ac_entities: set[str]) -> bool:
    if entity_id.startswith("climate."):
        return not _is_ac_climate(entity_id, known_ac_entities)
    lowered = entity_id.lower()
    if _is_ac_temp_entity(lowered):
        return False
    return any(tok in lowered for tok in ("actual_temperature", "trv", "etrv", "radiator", "petek"))


def _is_contact_record(rec: RegistryEntityRecord) -> bool:
    eid_low = rec.entity_id.lower()
    fname_norm = normalize_tr(rec.friendly_name)
    return any(kw in eid_low or kw in fname_norm for kw in _CONTACT_KEYWORDS)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class NeonPlanAdapter:
    """Parse NeonPlan 3D JSON exports into a geometry-aware :class:`BuildingTopology`."""

    def __init__(
        self,
        default_ceiling_height_m: float = 2.60,
        radiator_proximity_threshold_m: float = 0.50,
        default_etrv_bias_factor: float = 2.5,
    ) -> None:
        self._default_ceiling_height_m = default_ceiling_height_m
        self._radiator_proximity_threshold_m = radiator_proximity_threshold_m
        self._default_etrv_bias_factor = default_etrv_bias_factor

    def load(
        self,
        json_path: str | Path,
        overrides_path: str | Path | None = None,
        registry_records: Sequence[RegistryEntityRecord] | None = None,
    ) -> BuildingTopology:
        """Load NeonPlan JSON and optional YAML overrides from disk."""
        plan_file = Path(json_path)
        if not plan_file.exists():
            raise FileNotFoundError(f"NeonPlan JSON not found: {plan_file}")
        raw_payload = json.loads(plan_file.read_text(encoding="utf-8"))
        if not isinstance(raw_payload, Mapping):
            raise TypeError(f"NeonPlan JSON root must be a mapping: {plan_file}")

        overrides: dict[str, Any] | None = None
        if overrides_path is not None:
            ov_file = Path(overrides_path)
            if ov_file.exists():
                loaded_yaml = yaml.safe_load(ov_file.read_text(encoding="utf-8"))
                if isinstance(loaded_yaml, Mapping):
                    overrides = dict(loaded_yaml)
            else:
                logger.debug(
                    "thermal_overrides file not found at '%s'; continuing without overrides", ov_file
                )

        return self.parse_dict(raw_payload, overrides=overrides, registry_records=registry_records)

    def parse_dict(
        self,
        payload: Mapping[str, Any],
        overrides: Mapping[str, Any] | None = None,
        registry_records: Sequence[RegistryEntityRecord] | None = None,
    ) -> BuildingTopology:
        """Parse an in-memory NeonPlan dictionary with optional overrides and HA registry records."""
        building = payload.get("building")
        root: Mapping[str, Any] = building if isinstance(building, Mapping) else payload
        ov_map: Mapping[str, Any] = overrides or {}

        version = int(_as_float(payload.get("version") or root.get("version"), 1.0) or 1)
        weather_entity = _as_str(
            ov_map.get("weather_entity")
            or payload.get("weather_entity")
            or root.get("weather_entity")
            or root.get("weatherEntity")
        )
        north_azimuth = _as_float(
            ov_map.get("north_azimuth_deg")
            or payload.get("north_azimuth_deg")
            or root.get("north_azimuth_deg")
            or root.get("northAzimuthDeg")
        )
        outdoor_sensors: list[str] = []
        for src in (
            ov_map.get("outdoor_temp_sensors"),
            payload.get("outdoor_temp_sensors"),
            root.get("outdoor_temp_sensors"),
            root.get("outdoorTempSensors"),
        ):
            if isinstance(src, Sequence) and not isinstance(src, (str, bytes)):
                for item in src:
                    _append_unique(outdoor_sensors, _as_str(item))

        raw_floors = self._extract_floors(payload, root)
        zones: dict[str, ZoneTopology] = {}
        room_id_to_zone_id: dict[str, str] = {}
        floor_zone_ids: dict[str, list[str]] = {}
        used_zone_ids: set[str] = set()
        ac_climate_entities: set[str] = set()
        wall_sensor_entities: set[str] = set()

        default_ceil = _as_float(ov_map.get("default_ceiling_height_m"), self._default_ceiling_height_m)
        assert default_ceil is not None

        # Pass 1: Build zones per floor
        for floor_idx, floor in enumerate(raw_floors):
            floor_id = _as_str(floor.get("id") or floor.get("floor_id")) or f"floor_{floor_idx}"
            floor_name = _as_str(floor.get("name") or floor.get("floor_name")) or floor_id
            floor_height = _as_float(
                floor.get("height") or floor.get("ceiling_height") or floor.get("ceiling_height_m"),
                default_ceil,
            )
            assert floor_height is not None
            if floor_height <= 0.0:
                floor_height = default_ceil

            floor_zone_ids.setdefault(floor_id, [])
            rooms = floor.get("rooms")
            if not isinstance(rooms, Sequence) or isinstance(rooms, (str, bytes)):
                continue

            for room_idx, room in enumerate(rooms):
                if not isinstance(room, Mapping):
                    continue
                room_id = _as_str(room.get("id") or room.get("room_id")) or f"{floor_id}_room_{room_idx}"
                room_name = _as_str(room.get("name")) or room_id
                area_id = _as_str(
                    room.get("area_id")
                    or room.get("areaId")
                    or room.get("ha_area_id")
                    or room.get("haAreaId")
                )
                zone_id = canonical_zone_id(room_name, used_zone_ids)
                used_zone_ids.add(zone_id)

                polygon = _extract_polygon(room)
                area_m2 = compute_polygon_area(polygon) if len(polygon) >= 3 else 0.0
                if area_m2 <= 0.0:
                    explicit_area = _as_float(
                        room.get("floor_area_m2") or room.get("area_m2") or room.get("area"), 0.0
                    )
                    area_m2 = max(0.0, explicit_area or 0.0)

                room_ceil = _as_float(
                    room.get("ceiling_height_m") or room.get("ceiling_height") or room.get("height"),
                    floor_height,
                )
                assert room_ceil is not None
                if room_ceil <= 0.0:
                    room_ceil = floor_height

                zone = ZoneTopology(
                    zone_id=zone_id,
                    room_id=room_id,
                    name=room_name,
                    floor_id=floor_id,
                    floor_name=floor_name,
                    area_id=area_id,
                    polygon=polygon,
                    floor_area_m2=round(area_m2, 4),
                    ceiling_height_m=round(room_ceil, 4),
                    volume_m3=round(area_m2 * room_ceil, 4),
                )
                self._extract_room_direct_entities(room, zone)
                zones[zone_id] = zone
                room_id_to_zone_id[room_id] = zone_id
                floor_zone_ids[floor_id].append(zone_id)

                # Room-scoped openings / furniture / placements if nested
                self._process_openings(
                    room.get("openings"),
                    zones,
                    [zone_id],
                    room_id_to_zone_id,
                    default_zone_id=zone_id,
                )
                self._process_furniture(
                    room.get("furniture"),
                    zones,
                    [zone_id],
                    room_id_to_zone_id,
                    ac_climate_entities,
                    wall_sensor_entities,
                    default_zone_id=zone_id,
                )
                self._process_placements(
                    room.get("placements"),
                    zones,
                    [zone_id],
                    room_id_to_zone_id,
                    default_zone_id=zone_id,
                )

        # Pass 2: Shared & external walls per floor
        for z_ids in floor_zone_ids.values():
            self._compute_floor_wall_topology([zones[zid] for zid in z_ids])

        # Pass 3: Floor-scoped openings, furniture, placements
        for floor_idx, floor in enumerate(raw_floors):
            floor_id = _as_str(floor.get("id") or floor.get("floor_id")) or f"floor_{floor_idx}"
            z_ids = floor_zone_ids.get(floor_id, [])
            self._process_openings(floor.get("openings"), zones, z_ids, room_id_to_zone_id)
            self._process_furniture(
                floor.get("furniture"),
                zones,
                z_ids,
                room_id_to_zone_id,
                ac_climate_entities,
                wall_sensor_entities,
            )
            self._process_placements(floor.get("placements"), zones, z_ids, room_id_to_zone_id)

        # Pass 4: Building / top-level placements, openings, furniture
        all_zone_ids = list(zones.keys())
        containers: list[Mapping[str, Any]] = [root] if root is payload else [root, payload]
        for container in containers:
            self._process_openings(container.get("openings"), zones, all_zone_ids, room_id_to_zone_id)
            self._process_furniture(
                container.get("furniture"),
                zones,
                all_zone_ids,
                room_id_to_zone_id,
                ac_climate_entities,
                wall_sensor_entities,
            )
            self._process_placements(
                container.get("placements"), zones, all_zone_ids, room_id_to_zone_id
            )

        # Pass 5: Enrich from Home Assistant registry records
        if registry_records:
            self._enrich_from_registry(zones, registry_records)

        # Pass 6: Apply thermal_overrides.yaml
        self._apply_overrides(zones, room_id_to_zone_id, ov_map)

        # Pass 7: Synthesize virtual eTRV sensors from climate_entities & classify all sensors
        for zone in zones.values():
            self._finalize_zone_sensors(zone, ac_climate_entities, wall_sensor_entities)

        return BuildingTopology(
            version=version,
            weather_entity=weather_entity,
            outdoor_temp_sensors=outdoor_sensors,
            north_azimuth_deg=north_azimuth,
            zones=zones,
        )

    @staticmethod
    def _extract_floors(payload: Mapping[str, Any], root: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        floors = root.get("floors") or payload.get("floors")
        if isinstance(floors, Sequence) and not isinstance(floors, (str, bytes)):
            return [f for f in floors if isinstance(f, Mapping)]
        rooms = root.get("rooms") or payload.get("rooms")
        if isinstance(rooms, Sequence) and not isinstance(rooms, (str, bytes)):
            return [
                {
                    "id": "floor_0",
                    "name": "Floor 0",
                    "rooms": list(rooms),
                }
            ]
        return []

    @staticmethod
    def _extract_room_direct_entities(room: Mapping[str, Any], zone: ZoneTopology) -> None:
        climate_val = room.get("climate")
        if isinstance(climate_val, str):
            eid = _as_str(climate_val)
            if eid:
                _append_unique(zone.climate_entities, eid)
        elif isinstance(climate_val, Mapping):
            for key in ("climate", "entity_id", "thermostat"):
                raw = climate_val.get(key)
                if isinstance(raw, str):
                    _append_unique(zone.climate_entities, _as_str(raw))
                elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                    for item in raw:
                        _append_unique(zone.climate_entities, _as_str(item))
            for temp_key in ("temperature", "temp"):
                t_raw = climate_val.get(temp_key)
                t_list = (
                    [t_raw]
                    if isinstance(t_raw, str)
                    else (
                        list(t_raw)
                        if isinstance(t_raw, Sequence) and not isinstance(t_raw, (str, bytes))
                        else []
                    )
                )
                for item in t_list:
                    eid = _as_str(item)
                    if eid:
                        zone.temperature_sensors.append(
                            SensorPlacement(entity_id=eid, sensor_type="temperature", source="neonplan")
                        )
            hum_raw = climate_val.get("humidity")
            h_list = (
                [hum_raw]
                if isinstance(hum_raw, str)
                else (
                    list(hum_raw)
                    if isinstance(hum_raw, Sequence) and not isinstance(hum_raw, (str, bytes))
                    else []
                )
            )
            for item in h_list:
                _append_unique(zone.humidity_sensors, _as_str(item))
            for valve_key in ("valve", "valve_position"):
                v_raw = climate_val.get(valve_key)
                if isinstance(v_raw, str):
                    _append_unique(zone.valve_position_sensors, _as_str(v_raw))
            for contact_key in ("contact", "window"):
                c_raw = climate_val.get(contact_key)
                if isinstance(c_raw, str):
                    _append_unique(zone.contact_sensors, _as_str(c_raw))

        for eid in room.get("climate_entities") or []:
            _append_unique(zone.climate_entities, _as_str(eid))
        for eid in room.get("humidity_sensors") or []:
            _append_unique(zone.humidity_sensors, _as_str(eid))
        for eid in room.get("valve_position_sensors") or []:
            _append_unique(zone.valve_position_sensors, _as_str(eid))
        for eid in room.get("contact_sensors") or []:
            _append_unique(zone.contact_sensors, _as_str(eid))
        for s_item in room.get("temperature_sensors") or []:
            if isinstance(s_item, str):
                eid = _as_str(s_item)
                if eid:
                    zone.temperature_sensors.append(
                        SensorPlacement(entity_id=eid, sensor_type="temperature", source="neonplan")
                    )
            elif isinstance(s_item, Mapping):
                eid = _as_str(s_item.get("entity_id"))
                if eid:
                    sx, sy, sz = _extract_coords(s_item)
                    zone.temperature_sensors.append(
                        SensorPlacement(
                            entity_id=eid,
                            sensor_type="temperature",
                            x=sx,
                            y=sy,
                            z=sz,
                            source="neonplan",
                        )
                    )

    @staticmethod
    def _compute_floor_wall_topology(floor_zones: Sequence[ZoneTopology]) -> None:
        n = len(floor_zones)
        edge_shared: dict[str, list[float]] = {}
        for z in floor_zones:
            edge_shared[z.zone_id] = [0.0] * len(z.polygon)

        for i in range(n):
            z1 = floor_zones[i]
            if len(z1.polygon) < 3:
                continue
            for j in range(i + 1, n):
                z2 = floor_zones[j]
                if len(z2.polygon) < 3:
                    continue
                total_shared = 0.0
                for e1_idx in range(len(z1.polygon)):
                    p1 = z1.polygon[e1_idx]
                    p2 = z1.polygon[(e1_idx + 1) % len(z1.polygon)]
                    for e2_idx in range(len(z2.polygon)):
                        q1 = z2.polygon[e2_idx]
                        q2 = z2.polygon[(e2_idx + 1) % len(z2.polygon)]
                        ov = segment_overlap_length(p1, p2, q1, q2)
                        if ov > 0.0:
                            total_shared += ov
                            edge_shared[z1.zone_id][e1_idx] += ov
                            edge_shared[z2.zone_id][e2_idx] += ov
                if total_shared > 0.0:
                    rounded = round(total_shared, 3)
                    if z2.zone_id not in z1.adjacent_zones:
                        z1.adjacent_zones.append(z2.zone_id)
                    if z1.zone_id not in z2.adjacent_zones:
                        z2.adjacent_zones.append(z1.zone_id)
                    z1.shared_wall_lengths_m[z2.zone_id] = rounded
                    z2.shared_wall_lengths_m[z1.zone_id] = rounded

        for z in floor_zones:
            if len(z.polygon) < 3:
                continue
            ext_count = 0
            ext_length = 0.0
            for idx in range(len(z.polygon)):
                p1 = z.polygon[idx]
                p2 = z.polygon[(idx + 1) % len(z.polygon)]
                edge_len = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
                shared_len = min(edge_len, edge_shared[z.zone_id][idx])
                if shared_len < 0.5 * edge_len:
                    ext_count += 1
                    ext_length += max(0.0, edge_len - shared_len)
            z.external_walls = ext_count
            z.external_wall_length_m = round(ext_length, 3)

    @staticmethod
    def _find_zone_for_point(
        x: float,
        z: float,
        zones: Mapping[str, ZoneTopology],
        candidate_zone_ids: Sequence[str],
        max_edge_dist: float = _WALL_ASSIGN_TOL_M,
    ) -> ZoneTopology | None:
        inside_matches: list[ZoneTopology] = []
        for zid in candidate_zone_ids:
            zone = zones[zid]
            if len(zone.polygon) >= 3 and point_in_polygon(x, z, zone.polygon):
                inside_matches.append(zone)
        if inside_matches:
            inside_matches.sort(key=lambda item: item.floor_area_m2)
            return inside_matches[0]

        best_zone: ZoneTopology | None = None
        best_dist = max_edge_dist
        for zid in candidate_zone_ids:
            zone = zones[zid]
            if len(zone.polygon) < 2:
                continue
            dist = distance_to_polygon_edges(x, z, zone.polygon)
            if dist <= best_dist:
                best_dist = dist
                best_zone = zone
        return best_zone

    def _resolve_target_zone(
        self,
        item: Mapping[str, Any],
        zones: Mapping[str, ZoneTopology],
        candidate_zone_ids: Sequence[str],
        room_id_to_zone_id: Mapping[str, str],
        default_zone_id: str | None = None,
    ) -> ZoneTopology | None:
        raw_room_id = _as_str(item.get("room_id") or item.get("roomId"))
        if raw_room_id:
            if raw_room_id in room_id_to_zone_id:
                return zones.get(room_id_to_zone_id[raw_room_id])
            if raw_room_id in zones:
                return zones[raw_room_id]
        ix, _, iz = _extract_coords(item)
        if ix is not None and iz is not None:
            matched = self._find_zone_for_point(ix, iz, zones, candidate_zone_ids)
            if matched is not None:
                return matched
        if default_zone_id and default_zone_id in zones:
            return zones[default_zone_id]
        return None

    def _process_openings(
        self,
        raw_openings: Any,
        zones: Mapping[str, ZoneTopology],
        candidate_zone_ids: Sequence[str],
        room_id_to_zone_id: Mapping[str, str],
        default_zone_id: str | None = None,
    ) -> None:
        if not isinstance(raw_openings, Sequence) or isinstance(raw_openings, (str, bytes)):
            return
        for idx, item in enumerate(raw_openings):
            if not isinstance(item, Mapping):
                continue
            op_id = _as_str(item.get("id")) or f"opening_{idx}"
            raw_type = (_as_str(item.get("type") or item.get("opening_type")) or "window").lower()
            opening_type: Literal["window", "door"] = "window" if "window" in raw_type else "door"
            width_m = _as_float(item.get("width") or item.get("width_m") or item.get("w"), 1.0) or 1.0
            default_h = 1.2 if opening_type == "window" else 2.1
            height_m = (
                _as_float(item.get("height") or item.get("height_m") or item.get("h"), default_h)
                or default_h
            )
            area_m2 = round(max(0.0, width_m) * max(0.0, height_m), 4)

            contacts: list[str] = []
            for key in ("contact", "contact2", "sensor", "sensor2", "contact_entity"):
                _append_unique(contacts, _as_str(item.get(key)))
            extra_contacts = item.get("contact_entities")
            if isinstance(extra_contacts, Sequence) and not isinstance(extra_contacts, (str, bytes)):
                for c_item in extra_contacts:
                    _append_unique(contacts, _as_str(c_item))

            cover_entity = _as_str(
                item.get("cover") or item.get("cover_entity") or item.get("coverEntity")
            )
            shared_raw = _as_str(
                item.get("shared_with_zone") or item.get("to_room_id") or item.get("adjacent_room_id")
            )
            shared_zone = room_id_to_zone_id.get(shared_raw, shared_raw) if shared_raw else None

            zone = self._resolve_target_zone(
                item, zones, candidate_zone_ids, room_id_to_zone_id, default_zone_id
            )
            if zone is None:
                ix, _, iz = _extract_coords(item)
                if ix is not None and iz is not None:
                    zone = self._find_zone_for_point(
                        ix, iz, zones, candidate_zone_ids, max_edge_dist=_OPENING_ASSIGN_TOL_M
                    )
            if zone is None:
                continue

            if any(existing.id == op_id for existing in zone.openings):
                continue

            spec = OpeningSpec(
                id=op_id,
                opening_type=opening_type,
                width_m=round(width_m, 4),
                height_m=round(height_m, 4),
                area_m2=area_m2,
                contact_entities=contacts,
                cover_entity=cover_entity,
                shared_with_zone=shared_zone,
            )
            zone.openings.append(spec)
            if opening_type == "window":
                zone.window_area_m2 = round(zone.window_area_m2 + area_m2, 4)
            for c_eid in contacts:
                _append_unique(zone.contact_sensors, c_eid)

    def _process_furniture(
        self,
        raw_furniture: Any,
        zones: Mapping[str, ZoneTopology],
        candidate_zone_ids: Sequence[str],
        room_id_to_zone_id: Mapping[str, str],
        ac_climate_entities: set[str],
        wall_sensor_entities: set[str],
        default_zone_id: str | None = None,
    ) -> None:
        if not isinstance(raw_furniture, Sequence) or isinstance(raw_furniture, (str, bytes)):
            return
        for idx, item in enumerate(raw_furniture):
            if not isinstance(item, Mapping):
                continue
            zone = self._resolve_target_zone(
                item, zones, candidate_zone_ids, room_id_to_zone_id, default_zone_id
            )
            if zone is None:
                continue

            ftype = (
                _as_str(item.get("type") or item.get("furniture_type") or item.get("kind")) or ""
            ).lower()
            fid = _as_str(item.get("id")) or f"furn_{idx}"
            fx, fy, fz = _extract_coords(item)
            entity_id = _as_str(item.get("entity_id") or item.get("entityId") or item.get("entity"))
            climate_ent = _as_str(item.get("climate_entity") or item.get("climate"))
            valve_ent = _as_str(
                item.get("valve_sensor_entity") or item.get("valve_entity") or item.get("valve_sensor")
            )
            temp_ent = _as_str(item.get("temperature_entity") or item.get("temp_entity"))
            hum_ent = _as_str(item.get("humidity_entity"))

            if isinstance(item.get("entities"), Mapping):
                ents = item["entities"]
                climate_ent = climate_ent or _as_str(ents.get("climate"))
                valve_ent = valve_ent or _as_str(ents.get("valve") or ents.get("valve_position"))
                temp_ent = temp_ent or _as_str(ents.get("temperature"))
                hum_ent = hum_ent or _as_str(ents.get("humidity"))

            if "radiator" in ftype or "petek" in ftype or "radyator" in ftype:
                if climate_ent is None and entity_id and entity_id.startswith("climate."):
                    climate_ent = entity_id
                if valve_ent is None and entity_id and "valve" in entity_id.lower():
                    valve_ent = entity_id
                if fx is not None and fz is not None and not any(r.id == fid for r in zone.radiators):
                    width_m = (
                        _as_float(item.get("width") or item.get("width_m") or item.get("w"), 1.0) or 1.0
                    )
                    height_m = (
                        _as_float(item.get("height") or item.get("height_m") or item.get("h"), 0.6)
                        or 0.6
                    )
                    zone.radiators.append(
                        RadiatorPlacement(
                            id=fid,
                            x=fx,
                            z=fz,
                            width_m=width_m,
                            height_m=height_m,
                            climate_entity=climate_ent,
                            valve_sensor_entity=valve_ent,
                            furniture_type=ftype or "radiator",
                        )
                    )
                _append_unique(zone.climate_entities, climate_ent)
                _append_unique(zone.valve_position_sensors, valve_ent)
                if temp_ent:
                    zone.temperature_sensors.append(
                        SensorPlacement(
                            entity_id=temp_ent,
                            sensor_type="temperature",
                            x=fx,
                            y=fy,
                            z=fz,
                            source="neonplan",
                        )
                    )
                _append_unique(zone.humidity_sensors, hum_ent)

            elif ftype in ("ac_unit", "ac", "air_conditioner", "klima"):
                ac_eid = climate_ent or (
                    entity_id if entity_id and entity_id.startswith("climate.") else None
                )
                if ac_eid:
                    ac_climate_entities.add(ac_eid)
                    _append_unique(zone.climate_entities, ac_eid)
                if temp_ent:
                    zone.temperature_sensors.append(
                        SensorPlacement(
                            entity_id=temp_ent,
                            sensor_type="temperature",
                            x=fx,
                            y=fy,
                            z=fz,
                            source="neonplan",
                        )
                    )
                _append_unique(zone.humidity_sensors, hum_ent)

            elif ftype in ("wall_switch", "thermostat", "sensor", "temp_sensor"):
                if climate_ent:
                    _append_unique(zone.climate_entities, climate_ent)
                    if fx is not None and fz is not None:
                        wall_sensor_entities.add(climate_ent)
                        zone.temperature_sensors.append(
                            SensorPlacement(
                                entity_id=climate_ent,
                                sensor_type="temperature",
                                x=fx,
                                y=fy,
                                z=fz,
                                source="neonplan",
                            )
                        )
                if entity_id:
                    if entity_id.startswith("climate."):
                        _append_unique(zone.climate_entities, entity_id)
                        if fx is not None and fz is not None:
                            wall_sensor_entities.add(entity_id)
                            zone.temperature_sensors.append(
                                SensorPlacement(
                                    entity_id=entity_id,
                                    sensor_type="temperature",
                                    x=fx,
                                    y=fy,
                                    z=fz,
                                    source="neonplan",
                                )
                            )
                    elif entity_id.startswith("binary_sensor."):
                        if any(kw in entity_id.lower() for kw in _CONTACT_KEYWORDS):
                            _append_unique(zone.contact_sensors, entity_id)
                    elif entity_id.startswith("sensor.") and not _is_noise_entity(entity_id):
                        eid_low_f = entity_id.lower()
                        if "humidity" in eid_low_f or "nem" in eid_low_f:
                            _append_unique(zone.humidity_sensors, entity_id)
                        elif "valve" in eid_low_f:
                            _append_unique(zone.valve_position_sensors, entity_id)
                        elif (
                            ftype in ("thermostat", "sensor", "temp_sensor")
                            or "temperature" in eid_low_f
                            or "temp" in eid_low_f
                            or "termometre" in eid_low_f
                            or "sicaklik" in eid_low_f
                        ):
                            if fx is not None and fz is not None:
                                wall_sensor_entities.add(entity_id)
                            zone.temperature_sensors.append(
                                SensorPlacement(
                                    entity_id=entity_id,
                                    sensor_type="temperature",
                                    x=fx,
                                    y=fy,
                                    z=fz,
                                    source="neonplan",
                                )
                            )
                if temp_ent:
                    if fx is not None and fz is not None:
                        wall_sensor_entities.add(temp_ent)
                    zone.temperature_sensors.append(
                        SensorPlacement(
                            entity_id=temp_ent,
                            sensor_type="temperature",
                            x=fx,
                            y=fy,
                            z=fz,
                            source="neonplan",
                        )
                    )
                _append_unique(zone.humidity_sensors, hum_ent)

    def _process_placements(
        self,
        raw_placements: Any,
        zones: Mapping[str, ZoneTopology],
        candidate_zone_ids: Sequence[str],
        room_id_to_zone_id: Mapping[str, str],
        default_zone_id: str | None = None,
    ) -> None:
        items: list[Mapping[str, Any]] = []
        if isinstance(raw_placements, Sequence) and not isinstance(raw_placements, (str, bytes)):
            items = [p for p in raw_placements if isinstance(p, Mapping)]
        elif isinstance(raw_placements, Mapping):
            for eid, val in raw_placements.items():
                if isinstance(val, Mapping):
                    merged = dict(val)
                    merged.setdefault("entity_id", eid)
                    items.append(merged)

        for item in items:
            entity_id = _as_str(item.get("entity_id") or item.get("entityId") or item.get("entity"))
            if not entity_id:
                continue
            zone = self._resolve_target_zone(
                item, zones, candidate_zone_ids, room_id_to_zone_id, default_zone_id
            )
            if zone is None:
                continue

            px, py, pz = _extract_coords(item)
            hint = (
                _as_str(item.get("sensor_type") or item.get("type") or item.get("device_class")) or ""
            ).lower()
            icon_val = (_as_str(item.get("icon")) or "").lower()
            eid_low = entity_id.lower()

            if _is_noise_entity(entity_id):
                continue

            if entity_id.startswith("climate."):
                _append_unique(zone.climate_entities, entity_id)
                if px is not None and pz is not None:
                    zone.temperature_sensors.append(
                        SensorPlacement(
                            entity_id=entity_id,
                            sensor_type="temperature",
                            x=px,
                            y=py,
                            z=pz,
                            source="neonplan",
                        )
                    )
            elif (
                (entity_id.startswith("binary_sensor.") and any(kw in eid_low for kw in _CONTACT_KEYWORDS))
                or hint == "contact"
            ):
                _append_unique(zone.contact_sensors, entity_id)
            elif entity_id.startswith("number.") and "max_valve" in eid_low:
                if zone.max_valve_entity is None:
                    zone.max_valve_entity = entity_id
            elif entity_id.startswith("sensor."):
                if hint == "humidity" or "humidity" in eid_low or "nem" in eid_low:
                    _append_unique(zone.humidity_sensors, entity_id)
                elif hint == "valve_position" or "valve" in eid_low:
                    _append_unique(zone.valve_position_sensors, entity_id)
                elif (
                    hint == "temperature"
                    or "thermometer" in icon_val
                    or any(kw in eid_low for kw in ("temperature", "temp", "termometre", "sicaklik"))
                ):
                    zone.temperature_sensors.append(
                        SensorPlacement(
                            entity_id=entity_id,
                            sensor_type="temperature",
                            x=px,
                            y=py,
                            z=pz,
                            source="neonplan",
                        )
                    )

    @staticmethod
    def _match_registry_record_to_zone(
        rec: RegistryEntityRecord,
        zones: Mapping[str, ZoneTopology],
    ) -> ZoneTopology | None:
        rec_area_norm = normalize_tr(rec.area_id or "")
        rec_area_name_norm = normalize_tr(rec.area_name or "")
        if not rec_area_norm and not rec_area_name_norm:
            return None

        exact_area_matches: list[ZoneTopology] = []
        fallback_matches: list[ZoneTopology] = []
        for zone in zones.values():
            if zone.area_id:
                z_area_norm = normalize_tr(zone.area_id)
                if (rec_area_norm and z_area_norm == rec_area_norm) or (
                    rec_area_name_norm and z_area_norm == rec_area_name_norm
                ):
                    exact_area_matches.append(zone)
                    continue
            z_name_norm = normalize_tr(zone.name)
            z_id_norm = normalize_tr(zone.zone_id.replace("_", " "))
            if (rec_area_norm and rec_area_norm in (z_name_norm, z_id_norm)) or (
                rec_area_name_norm and rec_area_name_norm in (z_name_norm, z_id_norm)
            ):
                fallback_matches.append(zone)

        candidates = exact_area_matches or fallback_matches
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        rec_text = normalize_tr(f"{rec.entity_id.split('.', 1)[-1]} {rec.friendly_name}")
        rec_tokens = set(rec_text.split())

        def _score_candidate(z: ZoneTopology) -> tuple[int, float]:
            z_tokens = set(normalize_tr(z.name).split())
            overlap = len(rec_tokens & z_tokens)
            return (overlap, z.floor_area_m2)

        return max(candidates, key=_score_candidate)

    def _enrich_from_registry(
        self,
        zones: Mapping[str, ZoneTopology],
        registry_records: Sequence[RegistryEntityRecord],
    ) -> None:
        for rec in registry_records:
            if rec.disabled:
                continue
            if _is_noise_entity(rec.entity_id):
                continue
            zone = self._match_registry_record_to_zone(rec, zones)
            if zone is None:
                continue

            eid_low = rec.entity_id.lower()
            fname_norm = normalize_tr(rec.friendly_name)

            if rec.domain == "climate":
                _append_unique(zone.climate_entities, rec.entity_id)
            elif rec.domain == "number" and "max_valve" in eid_low:
                if zone.max_valve_entity is None:
                    zone.max_valve_entity = rec.entity_id
            elif rec.domain == "binary_sensor" and _is_contact_record(rec):
                _append_unique(zone.contact_sensors, rec.entity_id)
            elif rec.domain == "sensor":
                if "valve" in eid_low or "vana" in fname_norm or "valf" in fname_norm:
                    _append_unique(zone.valve_position_sensors, rec.entity_id)
                elif "humidity" in eid_low or "nem" in fname_norm or "rutubet" in fname_norm:
                    _append_unique(zone.humidity_sensors, rec.entity_id)
                elif (
                    "temperature" in eid_low
                    or "temp" in eid_low
                    or "sicaklik" in fname_norm
                    or "termometre" in fname_norm
                ) and not any(s.entity_id == rec.entity_id for s in zone.temperature_sensors):
                    zone.temperature_sensors.append(
                        SensorPlacement(
                            entity_id=rec.entity_id,
                            sensor_type="temperature",
                            source="registry",
                        )
                    )

    @staticmethod
    def _apply_overrides(
        zones: Mapping[str, ZoneTopology],
        room_id_to_zone_id: Mapping[str, str],
        overrides: Mapping[str, Any],
    ) -> None:
        raw_zones = overrides.get("zones")
        if not isinstance(raw_zones, Mapping):
            return

        by_norm_name: dict[str, str] = {normalize_tr(z.name): z.zone_id for z in zones.values()}
        for raw_key, raw_ov in raw_zones.items():
            if not isinstance(raw_ov, Mapping):
                continue
            ov = ZoneOverride.model_validate(raw_ov)
            lookup_key = ov.zone_id or str(raw_key)
            target_zid = (
                lookup_key
                if lookup_key in zones
                else room_id_to_zone_id.get(lookup_key) or by_norm_name.get(normalize_tr(lookup_key))
            )
            if target_zid is None or target_zid not in zones:
                continue
            zone = zones[target_zid]
            if ov.ceiling_height_m is not None and ov.ceiling_height_m > 0.0:
                zone.ceiling_height_m = round(ov.ceiling_height_m, 4)
                zone.volume_m3 = round(zone.floor_area_m2 * zone.ceiling_height_m, 4)
            if ov.insulation_factor is not None:
                zone.insulation_factor = ov.insulation_factor
            if ov.exterior_exposure is not None:
                zone.exterior_exposure = ov.exterior_exposure
            if ov.exclude:
                zone.excluded = True
            if ov.max_valve_entity is not None:
                zone.max_valve_entity = ov.max_valve_entity
            for eid in ov.extra_climate_entities:
                _append_unique(zone.climate_entities, eid)
            for eid in ov.extra_humidity_sensors:
                _append_unique(zone.humidity_sensors, eid)
            for eid in ov.extra_contact_sensors:
                _append_unique(zone.contact_sensors, eid)
            for eid in ov.extra_valve_sensors:
                _append_unique(zone.valve_position_sensors, eid)
            for eid in ov.extra_temperature_sensors:
                if not any(s.entity_id == eid for s in zone.temperature_sensors):
                    zone.temperature_sensors.append(
                        SensorPlacement(entity_id=eid, sensor_type="temperature", source="override")
                    )
            if ov.exclude_entities:
                excluded_set = set(ov.exclude_entities)
                zone.climate_entities = [e for e in zone.climate_entities if e not in excluded_set]
                zone.temperature_sensors = [
                    s for s in zone.temperature_sensors if s.entity_id not in excluded_set
                ]
                zone.humidity_sensors = [e for e in zone.humidity_sensors if e not in excluded_set]
                zone.valve_position_sensors = [
                    e for e in zone.valve_position_sensors if e not in excluded_set
                ]
                zone.contact_sensors = [e for e in zone.contact_sensors if e not in excluded_set]

    def _finalize_zone_sensors(
        self,
        zone: ZoneTopology,
        ac_climate_entities: set[str],
        wall_sensor_entities: set[str],
    ) -> None:
        # Deduplicate existing temperature sensors, preferring entries with coordinates.
        deduped: dict[str, SensorPlacement] = {}
        for sp in zone.temperature_sensors:
            existing = deduped.get(sp.entity_id)
            if existing is None or (
                (existing.x is None or existing.z is None) and (sp.x is not None and sp.z is not None)
            ):
                deduped[sp.entity_id] = sp

        # Synthesize virtual eTRV temperature readouts from non-AC climate entities.
        rad_by_climate: dict[str, RadiatorPlacement] = {
            r.climate_entity: r for r in zone.radiators if r.climate_entity
        }
        for cid in zone.climate_entities:
            if _is_ac_climate(cid, ac_climate_entities):
                continue
            if cid in deduped:
                continue
            rad = rad_by_climate.get(cid)
            deduped[cid] = SensorPlacement(
                entity_id=cid,
                sensor_type="temperature",
                x=rad.x if rad is not None else None,
                z=rad.z if rad is not None else None,
                source="radiator_climate" if rad is not None else "climate_virtual",
            )

        classified: list[SensorPlacement] = []
        for sp in deduped.values():
            if sp.x is not None and sp.z is not None and zone.radiators:
                min_dist = min(math.hypot(sp.x - r.x, sp.z - r.z) for r in zone.radiators)
                sp.distance_to_radiator_m = round(min_dist, 4)
                if min_dist < self._radiator_proximity_threshold_m:
                    sp.placement = "on_radiator"
                    sp.radiation_bias_factor = self._default_etrv_bias_factor
                    sp.reliability_weight = 1.0
                else:
                    sp.placement = "ambient_room"
                    sp.radiation_bias_factor = 0.0
                    sp.reliability_weight = 0.3 if _is_ac_temp_entity(sp.entity_id) else 1.0
            else:
                if _is_ac_temp_entity(sp.entity_id):
                    sp.placement = "ambient_room"
                    sp.radiation_bias_factor = 0.0
                    sp.reliability_weight = 0.3
                elif (
                    sp.entity_id in wall_sensor_entities
                    and "actual_temperature" not in sp.entity_id.lower()
                    and "trv" not in sp.entity_id.lower()
                ):
                    sp.placement = "ambient_room"
                    sp.radiation_bias_factor = 0.0
                    sp.reliability_weight = 1.0
                elif sp.source in ("radiator_climate", "climate_virtual") or _is_etrv_entity(
                    sp.entity_id, ac_climate_entities
                ):
                    sp.placement = "on_radiator"
                    sp.radiation_bias_factor = self._default_etrv_bias_factor
                    sp.reliability_weight = 1.0
                else:
                    sp.placement = "ambient_room"
                    sp.radiation_bias_factor = 0.0
                    sp.reliability_weight = 1.0
            classified.append(sp)

        zone.temperature_sensors = classified
