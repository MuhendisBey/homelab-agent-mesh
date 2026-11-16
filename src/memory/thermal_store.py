"""SQLite WAL persistence for per-zone thermal models and diagnostic observations."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ThermalZoneModelRecord(BaseModel):
    """Calibrated grey-box RC parameters and bias factors for a single thermal zone."""

    zone_id: str
    volume_m3: float
    k_loss: float = 0.08
    etrv_bias_factor: float = 2.5
    static_offset_c: float = 0.0
    avg_warmup_rate_c_per_h: float | None = None
    avg_cooling_rate_c_per_h: float | None = None
    updated_at: str = Field(default_factory=_utc_now_iso)


class ThermalObservationRecord(BaseModel):
    """Persisted physical diagnostic event (night cooling or ventilation shock)."""

    id: int | None = None
    zone_id: str
    event_type: Literal["night_cooling", "ventilation_shock"]
    started_at: str
    duration_min: float
    outdoor_temp_c: float | None = None
    outdoor_humidity: float | None = None
    indoor_temp_start_c: float
    indoor_temp_end_c: float
    indoor_abs_humidity_drop: float | None = None
    rebound_temp_c: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ThermalStore:
    """Async-safe SQLite WAL store for thermal zone parameters and observations."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._write_lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db_sync(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS thermal_zone_models (
                    zone_id TEXT PRIMARY KEY,
                    volume_m3 REAL NOT NULL,
                    k_loss REAL NOT NULL,
                    etrv_bias_factor REAL NOT NULL,
                    static_offset_c REAL NOT NULL,
                    avg_warmup_rate_c_per_h REAL,
                    avg_cooling_rate_c_per_h REAL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS thermal_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    duration_min REAL NOT NULL,
                    outdoor_temp_c REAL,
                    outdoor_humidity REAL,
                    indoor_temp_start_c REAL NOT NULL,
                    indoor_temp_end_c REAL NOT NULL,
                    indoor_abs_humidity_drop REAL,
                    rebound_temp_c REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(zone_id, event_type, started_at)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_thermal_observations_zone_type
                ON thermal_observations (zone_id, event_type, started_at DESC)
                """
            )
        self._initialized = True

    def _ensure_initialized_sync(self) -> None:
        if not self._initialized:
            self._init_db_sync()

    async def initialize(self) -> None:
        """Create thermal SQLite tables and enable WAL mode."""
        async with self._write_lock:
            await asyncio.to_thread(self._init_db_sync)

    @staticmethod
    def _row_to_zone_model(row: tuple[Any, ...]) -> ThermalZoneModelRecord:
        return ThermalZoneModelRecord(
            zone_id=str(row[0]),
            volume_m3=float(row[1]),
            k_loss=float(row[2]),
            etrv_bias_factor=float(row[3]),
            static_offset_c=float(row[4]),
            avg_warmup_rate_c_per_h=float(row[5]) if row[5] is not None else None,
            avg_cooling_rate_c_per_h=float(row[6]) if row[6] is not None else None,
            updated_at=str(row[7]),
        )

    @staticmethod
    def _row_to_observation(row: tuple[Any, ...]) -> ThermalObservationRecord:
        raw_meta = str(row[11]) if row[11] is not None else "{}"
        try:
            parsed = json.loads(raw_meta)
            metadata = dict(parsed) if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            metadata = {}
        event_type_raw = str(row[2])
        event_type: Literal["night_cooling", "ventilation_shock"] = (
            "night_cooling" if event_type_raw == "night_cooling" else "ventilation_shock"
        )
        return ThermalObservationRecord(
            id=int(row[0]),
            zone_id=str(row[1]),
            event_type=event_type,
            started_at=str(row[3]),
            duration_min=float(row[4]),
            outdoor_temp_c=float(row[5]) if row[5] is not None else None,
            outdoor_humidity=float(row[6]) if row[6] is not None else None,
            indoor_temp_start_c=float(row[7]),
            indoor_temp_end_c=float(row[8]),
            indoor_abs_humidity_drop=float(row[9]) if row[9] is not None else None,
            rebound_temp_c=float(row[10]) if row[10] is not None else None,
            metadata=metadata,
        )

    def _get_zone_model_sync(self, zone_id: str) -> ThermalZoneModelRecord | None:
        self._ensure_initialized_sync()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT zone_id, volume_m3, k_loss, etrv_bias_factor, static_offset_c,
                       avg_warmup_rate_c_per_h, avg_cooling_rate_c_per_h, updated_at
                FROM thermal_zone_models
                WHERE zone_id = ?
                """,
                (zone_id,),
            ).fetchone()
        return self._row_to_zone_model(row) if row is not None else None

    async def get_zone_model(self, zone_id: str) -> ThermalZoneModelRecord | None:
        """Fetch calibrated parameters for ``zone_id``, or ``None`` if absent."""
        return await asyncio.to_thread(self._get_zone_model_sync, zone_id)

    def _get_all_zone_models_sync(self) -> dict[str, ThermalZoneModelRecord]:
        self._ensure_initialized_sync()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT zone_id, volume_m3, k_loss, etrv_bias_factor, static_offset_c,
                       avg_warmup_rate_c_per_h, avg_cooling_rate_c_per_h, updated_at
                FROM thermal_zone_models
                ORDER BY zone_id ASC
                """
            ).fetchall()
        return {str(row[0]): self._row_to_zone_model(row) for row in rows}

    async def get_all_zone_models(self) -> dict[str, ThermalZoneModelRecord]:
        """Return all stored zone models keyed by ``zone_id``."""
        return await asyncio.to_thread(self._get_all_zone_models_sync)

    def _upsert_zone_model_sync(self, record: ThermalZoneModelRecord) -> ThermalZoneModelRecord:
        self._ensure_initialized_sync()
        updated_at = record.updated_at or _utc_now_iso()
        stored = record.model_copy(update={"updated_at": updated_at})
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO thermal_zone_models (
                    zone_id, volume_m3, k_loss, etrv_bias_factor, static_offset_c,
                    avg_warmup_rate_c_per_h, avg_cooling_rate_c_per_h, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(zone_id) DO UPDATE SET
                    volume_m3 = excluded.volume_m3,
                    k_loss = excluded.k_loss,
                    etrv_bias_factor = excluded.etrv_bias_factor,
                    static_offset_c = excluded.static_offset_c,
                    avg_warmup_rate_c_per_h = excluded.avg_warmup_rate_c_per_h,
                    avg_cooling_rate_c_per_h = excluded.avg_cooling_rate_c_per_h,
                    updated_at = excluded.updated_at
                """,
                (
                    stored.zone_id,
                    stored.volume_m3,
                    stored.k_loss,
                    stored.etrv_bias_factor,
                    stored.static_offset_c,
                    stored.avg_warmup_rate_c_per_h,
                    stored.avg_cooling_rate_c_per_h,
                    stored.updated_at,
                ),
            )
        return stored

    async def upsert_zone_model(self, record: ThermalZoneModelRecord) -> ThermalZoneModelRecord:
        """Insert or update a zone thermal model atomically."""
        async with self._write_lock:
            return await asyncio.to_thread(self._upsert_zone_model_sync, record)

    def _record_observation_sync(self, obs: ThermalObservationRecord) -> ThermalObservationRecord:
        self._ensure_initialized_sync()
        meta_json = json.dumps(obs.metadata, ensure_ascii=False, sort_keys=True)
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT id FROM thermal_observations
                WHERE zone_id = ? AND event_type = ? AND started_at = ?
                """,
                (obs.zone_id, obs.event_type, obs.started_at),
            ).fetchone()
            if existing is not None:
                obs_id = int(existing[0])
                conn.execute(
                    """
                    UPDATE thermal_observations
                    SET duration_min = ?, outdoor_temp_c = ?, outdoor_humidity = ?,
                        indoor_temp_start_c = ?, indoor_temp_end_c = ?,
                        indoor_abs_humidity_drop = ?, rebound_temp_c = ?, metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        obs.duration_min,
                        obs.outdoor_temp_c,
                        obs.outdoor_humidity,
                        obs.indoor_temp_start_c,
                        obs.indoor_temp_end_c,
                        obs.indoor_abs_humidity_drop,
                        obs.rebound_temp_c,
                        meta_json,
                        obs_id,
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO thermal_observations (
                        zone_id, event_type, started_at, duration_min, outdoor_temp_c,
                        outdoor_humidity, indoor_temp_start_c, indoor_temp_end_c,
                        indoor_abs_humidity_drop, rebound_temp_c, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        obs.zone_id,
                        obs.event_type,
                        obs.started_at,
                        obs.duration_min,
                        obs.outdoor_temp_c,
                        obs.outdoor_humidity,
                        obs.indoor_temp_start_c,
                        obs.indoor_temp_end_c,
                        obs.indoor_abs_humidity_drop,
                        obs.rebound_temp_c,
                        meta_json,
                    ),
                )
                obs_id = int(cur.lastrowid or 0)
        return obs.model_copy(update={"id": obs_id})

    async def record_observation(self, obs: ThermalObservationRecord) -> ThermalObservationRecord:
        """Persist a thermal observation idempotently by ``(zone_id, event_type, started_at)``."""
        async with self._write_lock:
            return await asyncio.to_thread(self._record_observation_sync, obs)

    def _list_observations_sync(
        self,
        zone_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[ThermalObservationRecord]:
        self._ensure_initialized_sync()
        clauses: list[str] = []
        params: list[Any] = []
        if zone_id is not None:
            clauses.append("zone_id = ?")
            params.append(zone_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        safe_limit = max(1, int(limit))
        params.append(safe_limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, zone_id, event_type, started_at, duration_min, outdoor_temp_c,
                       outdoor_humidity, indoor_temp_start_c, indoor_temp_end_c,
                       indoor_abs_humidity_drop, rebound_temp_c, metadata_json
                FROM thermal_observations
                {where_sql}
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._row_to_observation(row) for row in rows]

    async def list_observations(
        self,
        zone_id: str | None = None,
        event_type: Literal["night_cooling", "ventilation_shock"] | str | None = None,
        limit: int = 50,
    ) -> list[ThermalObservationRecord]:
        """Query recent thermal observations filtered optionally by zone and event type."""
        return await asyncio.to_thread(self._list_observations_sync, zone_id, event_type, limit)
