"""Ground-truth Home Assistant EntityResolver & CoALA Semantic Entity Memory.

Combines:
1. A 4-layer live Grounding Index synchronized with Home Assistant WebSocket registries
   (startup SQLite snapshot -> real-time ``*_registry_updated`` WS subscription ->
   reconnect resync -> 6-hour full reconciliation -> 404 on-use self-healing).
2. A CoALA semantic ``entity_memory`` table stored alongside ``ReflectionStore`` in
   SQLite, learning phrase -> entity mappings *only* after verified ``assert_state``
   success or explicit user correction, with Beta-Bernoulli confidence scoring and
   bidirectional synchronization when entities are renamed, moved, or removed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from src.core.config import AppConfig
from src.core.exceptions import HomeAssistantError
from src.core.text_tr import (
    fuzzy_phrase_similarity,
    infer_domain_hints,
    normalize_tr,
    stem_tr_phrase,
)
from src.memory.base import BaseEmbedder
from src.memory.reflection_store import (
    LocalHashedNgramEmbedder,
    _cosine_similarity,
)

logger = logging.getLogger(__name__)

_REGISTRY_EVENTS: tuple[str, ...] = (
    "entity_registry_updated",
    "device_registry_updated",
    "area_registry_updated",
    "floor_registry_updated",
)
_IGNORED_NOISE_DOMAINS: frozenset[str] = frozenset({"update", "persistent_notification", "zone", "sun"})
_LIGHT_SWITCH_DOMAINS: frozenset[str] = frozenset({"light", "switch"})
_DOMAIN_IMPLICIT_STEMS: dict[str, frozenset[str]] = {
    "light": frozenset({"isik", "lamba", "ampul", "aydinlatma", "spot", "led"}),
    "cover": frozenset({"perde", "panjur", "guneslik", "stor", "kepenk"}),
    "fan": frozenset({"fan", "vantilator", "havalandirma"}),
}
_PENDING_QUERY_TTL_SEC = 300.0


def _domains_compatible(explicit_domain: str | None, candidate_domain: str) -> bool:
    """Allow light<->switch cross-matching since users call smart-switch lights 'ışık'."""
    if not explicit_domain or explicit_domain == candidate_domain:
        return True
    return explicit_domain in _LIGHT_SWITCH_DOMAINS and candidate_domain in _LIGHT_SWITCH_DOMAINS


class HARegistryBackend(Protocol):
    """Minimal Home Assistant backend protocol required by :class:`EntityResolver`."""

    def ws_session(self) -> AbstractAsyncContextManager[Any]:
        """Yield an authenticated Home Assistant WebSocket connection."""
        ...

    async def send_ws_command(
        self,
        ws: Any,
        msg_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a WebSocket JSON command and wait for its matching ``result``."""
        ...

    async def get_all_states(self) -> list[dict[str, Any]]:
        """Fetch all entity states via REST ``/api/states``."""
        ...


class RegistryEntityRecord(BaseModel):
    """Normalized Home Assistant entity entry enriched with device/area/floor context."""

    entity_id: str
    domain: str
    name: str | None = None
    original_name: str | None = None
    friendly_name: str
    area_id: str | None = None
    area_name: str | None = None
    floor_id: str | None = None
    floor_name: str | None = None
    device_id: str | None = None
    device_name: str | None = None
    aliases: tuple[str, ...] = ()
    state: str | None = None
    disabled: bool = False
    search_text_norm: str = ""
    search_stems: tuple[str, ...] = ()
    area_stems: tuple[str, ...] = ()


MatchSource = Literal[
    "static_alias",
    "entity_memory_exact",
    "entity_memory_fuzzy",
    "entity_memory_semantic",
    "registry_grounding",
]


class EntityCandidate(BaseModel):
    """Ranked candidate returned by :meth:`EntityResolver.resolve`."""

    entity_id: str
    domain: str
    friendly_name: str
    area_id: str | None = None
    area_name: str | None = None
    state: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    match_source: MatchSource
    reason: str


class EntityResolutionResult(BaseModel):
    """Deterministic entity resolution outcome."""

    query: str
    normalized_query: str
    inferred_domains: list[str] = Field(default_factory=list)
    resolved: bool = False
    ambiguous: bool = False
    top_entity_id: str | None = None
    candidates: list[EntityCandidate] = Field(default_factory=list)
    elapsed_ms: float = 0.0


class EntityMemoryRecord(BaseModel):
    """Row from the CoALA ``entity_memory`` SQLite table."""

    id: int
    phrase: str
    phrase_norm: str
    phrase_stems: str
    entity_id: str
    domain: str
    area_id: str | None = None
    source: str
    success_count: int
    failure_count: int
    confidence: float
    status: str
    last_verified_at: str


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _beta_bernoulli_confidence(source: str, success_count: int, failure_count: int) -> float:
    prior_success = 2 if source in {"user_correction", "ha_alias", "static_alias"} else 1
    denom = success_count + failure_count + prior_success + 1
    return float((success_count + prior_success) / max(1, denom))


def _extract_domain(entity_id: str) -> str:
    if "." in entity_id:
        return entity_id.split(".", 1)[0].strip().lower()
    return ""


class EntityResolver:
    """Fast sub-10ms entity resolver combining live HA Grounding Index & CoALA semantic memory."""

    def __init__(
        self,
        config: AppConfig,
        db_path: str | Path,
        ha_backend: HARegistryBackend | None = None,
        embedder: BaseEmbedder | None = None,
        confidence_threshold: float = 0.85,
        reconcile_interval_sec: float = 21600.0,
        enable_ws_listener: bool = True,
    ) -> None:
        self._config = config
        self._db_path = Path(db_path)
        self._ha = ha_backend
        self._embedder = embedder
        self._fallback_embedder = LocalHashedNgramEmbedder(
            dimensions=config.memory.local_embedding_dimensions
        )
        self._confidence_threshold = confidence_threshold
        self._reconcile_interval_sec = reconcile_interval_sec
        self._enable_ws_listener = enable_ws_listener

        self._write_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._static_aliases: dict[str, str] = {
            normalize_tr(k, preserve_entity_chars=True): v.strip()
            for k, v in config.home_assistant.entity_aliases.items()
            if k.strip() and v.strip()
        }
        # Exact original keys lowercase too (e.g. living_room_tv)
        self._raw_aliases: dict[str, str] = {
            k.strip().lower(): v.strip() for k, v in config.home_assistant.entity_aliases.items()
        }

        self._records: dict[str, RegistryEntityRecord] = {}
        self._recent_resolutions: dict[str, tuple[str, float]] = {}
        self._exact_memory_aliases: dict[str, str] = {}

        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
        self._debounce_task: asyncio.Task[None] | None = None
        self._last_sync_utc: str | None = None
        self._last_sync_error: str | None = None

        self._init_db_sync()
        self._rebuild_exact_alias_cache_sync()

    # ------------------------------------------------------------------
    # SQLite schema & helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db_sync(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phrase TEXT NOT NULL,
                    phrase_norm TEXT NOT NULL,
                    phrase_stems TEXT NOT NULL,
                    embedding_model TEXT NOT NULL DEFAULT '',
                    embedding_json TEXT NOT NULL DEFAULT '[]',
                    entity_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    area_id TEXT,
                    source TEXT NOT NULL DEFAULT 'verified_action',
                    success_count INTEGER NOT NULL DEFAULT 1,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    last_verified_at TEXT NOT NULL,
                    UNIQUE(phrase_norm, entity_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_entity_memory_status_norm
                ON entity_memory (status, phrase_norm)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_entity_memory_entity_id
                ON entity_memory (entity_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_registry_snapshot (
                    entity_id TEXT PRIMARY KEY,
                    record_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _load_snapshot_sync(self) -> dict[str, RegistryEntityRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT entity_id, record_json FROM entity_registry_snapshot").fetchall()
        loaded: dict[str, RegistryEntityRecord] = {}
        for eid, raw_json in rows:
            try:
                loaded[str(eid)] = RegistryEntityRecord.model_validate_json(str(raw_json))
            except Exception:
                logger.warning("entity_id=<%s> | failed to decode cached registry snapshot", eid)
        return loaded

    def _save_snapshot_sync(self, records: Mapping[str, RegistryEntityRecord]) -> None:
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute("DELETE FROM entity_registry_snapshot")
            conn.executemany(
                "INSERT INTO entity_registry_snapshot (entity_id, record_json, updated_at) VALUES (?, ?, ?)",
                [(eid, rec.model_dump_json(), now) for eid, rec in records.items()],
            )

    def _delete_snapshot_entity_sync(self, entity_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM entity_registry_snapshot WHERE entity_id = ?", (entity_id,))

    # ------------------------------------------------------------------
    # Lifecycle & 4-layer background sync
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load cached snapshot immediately and start non-blocking HA registry sync."""
        self._init_db_sync()
        snapshot = await asyncio.to_thread(self._load_snapshot_sync)
        if snapshot:
            self._records = snapshot
            logger.info("entity_resolver | loaded %d entity record(s) from SQLite snapshot", len(snapshot))

        self._running = True
        if self._ha is not None:
            self._tasks.append(asyncio.create_task(self._initial_sync_safe(), name="er_initial_sync"))
            if self._enable_ws_listener:
                self._tasks.append(asyncio.create_task(self._ws_event_loop(), name="er_ws_listener"))
            if self._reconcile_interval_sec > 0:
                self._tasks.append(asyncio.create_task(self._reconcile_loop(), name="er_reconcile"))

    async def stop(self) -> None:
        """Cancel background sync tasks cleanly."""
        self._running = False
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()
            try:
                await self._debounce_task
            except asyncio.CancelledError:
                pass
            self._debounce_task = None

        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

    async def _initial_sync_safe(self) -> None:
        try:
            await self.refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_sync_error = str(exc)
            logger.warning("entity_resolver | initial registry sync deferred/failed: %s", exc)

    async def _reconcile_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._reconcile_interval_sec)
                if self._running:
                    await self.refresh()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._last_sync_error = str(exc)
                logger.warning("entity_resolver | periodic reconciliation failed: %s", exc)

    async def _ws_event_loop(self) -> None:
        """Layer 1 & Layer 2: real-time HA WS registry event stream with reconnect backoff."""
        assert self._ha is not None
        backoff = 2.0
        while self._running:
            try:
                async with self._ha.ws_session() as ws:
                    backoff = 2.0
                    sub_ids: dict[int, str] = {}
                    for idx, evt in enumerate(_REGISTRY_EVENTS, start=1):
                        await self._ha.send_ws_command(
                            ws,
                            idx,
                            {"type": "subscribe_events", "event_type": evt},
                        )
                        sub_ids[idx] = evt
                    logger.info("entity_resolver | subscribed to HA WS registry events: %s", list(_REGISTRY_EVENTS))
                    await self.refresh()

                    next_ping_id = len(_REGISTRY_EVENTS) + 1
                    while self._running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=60.0)
                        except TimeoutError:
                            ping_id = next_ping_id
                            next_ping_id += 1
                            await ws.send(json.dumps({"id": ping_id, "type": "ping"}))
                            continue

                        msg = json.loads(raw)
                        if msg.get("type") == "event":
                            event_obj = msg.get("event") or {}
                            event_type = str(event_obj.get("event_type") or "")
                            data = dict(event_obj.get("data") or {})
                            await self.handle_registry_event(event_type, data)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break
                logger.warning(
                    "entity_resolver | WS listener disconnected (%s); reconnecting in %.0fs",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2.0)

    def _schedule_debounced_refresh(self, delay_sec: float = 1.5) -> None:
        if not self._running or self._ha is None:
            return
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()

        async def _runner() -> None:
            try:
                await asyncio.sleep(delay_sec)
                await self.refresh()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("entity_resolver | debounced refresh failed: %s", exc)

        self._debounce_task = asyncio.create_task(_runner(), name="er_debounced_refresh")

    async def handle_registry_event(self, event_type: str, data: Mapping[str, Any]) -> None:
        """Apply immediate bidirectional updates to Grounding Index and ``entity_memory``."""
        action = str(data.get("action") or "").strip().lower()
        entity_id = str(data.get("entity_id") or "").strip()
        old_entity_id = str(data.get("old_entity_id") or "").strip()

        if event_type == "entity_registry_updated":
            if action == "remove" and entity_id:
                self._records.pop(entity_id, None)
                async with self._write_lock:
                    await asyncio.to_thread(self._delete_snapshot_entity_sync, entity_id)
                    await asyncio.to_thread(self._mark_entity_stale_sync, entity_id)
                logger.info("entity_resolver | entity removed '%s' -> marked entity_memory stale", entity_id)
            elif action == "update" and old_entity_id and entity_id and old_entity_id != entity_id:
                async with self._write_lock:
                    await asyncio.to_thread(self._migrate_entity_id_sync, old_entity_id, entity_id)
                old_rec = self._records.pop(old_entity_id, None)
                if old_rec is not None:
                    self._records[entity_id] = old_rec.model_copy(
                        update={"entity_id": entity_id, "domain": _extract_domain(entity_id)}
                    )
                logger.info(
                    "entity_resolver | entity renamed '%s' -> '%s'; migrated entity_memory",
                    old_entity_id,
                    entity_id,
                )

        if self._ha is not None:
            self._schedule_debounced_refresh()

    async def refresh(self) -> int:
        """Fetch live registries + states from HA, rebuild Grounding Index, and reconcile memory."""
        if self._ha is None:
            return len(self._records)

        async with self._refresh_lock:
            floors: list[dict[str, Any]] = []
            areas: list[dict[str, Any]] = []
            devices: list[dict[str, Any]] = []
            entities: list[dict[str, Any]] = []
            states: list[dict[str, Any]] = []

            ws_fetched_states = False
            try:
                async with self._ha.ws_session() as ws:
                    floors = await self._safe_ws_list(ws, 1, "config/floor_registry/list")
                    areas = await self._safe_ws_list(ws, 2, "config/area_registry/list")
                    devices = await self._safe_ws_list(ws, 3, "config/device_registry/list")
                    entities = await self._safe_ws_list(ws, 4, "config/entity_registry/list")
                    states = await self._safe_ws_list(ws, 5, "get_states")
                    ws_fetched_states = bool(states)
            except HomeAssistantError as exc:
                logger.warning("entity_resolver | WS registry fetch unavailable, falling back to REST states: %s", exc)

            if not ws_fetched_states:
                states = await self._ha.get_all_states()

            rebuilt = self._build_records(
                floors=floors,
                areas=areas,
                devices=devices,
                entities=entities,
                states=states,
            )
            self._records = rebuilt
            self._last_sync_utc = _utc_now_iso()
            self._last_sync_error = None

            async with self._write_lock:
                await asyncio.to_thread(self._save_snapshot_sync, rebuilt)
                if rebuilt:
                    await asyncio.to_thread(self._reconcile_memory_with_records_sync, rebuilt)

            logger.info("entity_resolver | refreshed Grounding Index (%d entities)", len(rebuilt))
            return len(rebuilt)

    async def _safe_ws_list(self, ws: Any, msg_id: int, cmd_type: str) -> list[dict[str, Any]]:
        assert self._ha is not None
        try:
            reply = await self._ha.send_ws_command(ws, msg_id, {"type": cmd_type})
            res = reply.get("result")
            return list(res) if isinstance(res, list) else []
        except HomeAssistantError as exc:
            logger.debug("entity_resolver | WS command '%s' skipped: %s", cmd_type, exc)
            return []

    @staticmethod
    def _build_records(
        floors: list[dict[str, Any]],
        areas: list[dict[str, Any]],
        devices: list[dict[str, Any]],
        entities: list[dict[str, Any]],
        states: list[dict[str, Any]],
    ) -> dict[str, RegistryEntityRecord]:
        floor_by_id: dict[str, dict[str, Any]] = {
            str(f.get("floor_id")): f for f in floors if f.get("floor_id")
        }
        area_by_id: dict[str, dict[str, Any]] = {
            str(a.get("area_id")): a for a in areas if a.get("area_id")
        }
        device_by_id: dict[str, dict[str, Any]] = {
            str(d.get("id")): d for d in devices if d.get("id")
        }
        reg_by_eid: dict[str, dict[str, Any]] = {
            str(e.get("entity_id")): e for e in entities if e.get("entity_id")
        }
        state_by_eid: dict[str, dict[str, Any]] = {
            str(s.get("entity_id")): s for s in states if s.get("entity_id")
        }

        all_entity_ids = set(reg_by_eid.keys()) | set(state_by_eid.keys())
        records: dict[str, RegistryEntityRecord] = {}
        for eid in sorted(all_entity_ids):
            domain = _extract_domain(eid)
            reg = reg_by_eid.get(eid) or {}
            st = state_by_eid.get(eid) or {}
            attrs = dict(st.get("attributes") or {})

            device_id = str(reg.get("device_id")) if reg.get("device_id") else None
            dev = device_by_id.get(device_id) if device_id else None

            area_id = (
                str(reg.get("area_id"))
                if reg.get("area_id")
                else (str(dev.get("area_id")) if dev and dev.get("area_id") else None)
            )
            area = area_by_id.get(area_id) if area_id else None
            area_name = str(area.get("name")) if area and area.get("name") else None

            floor_id = str(area.get("floor_id")) if area and area.get("floor_id") else None
            floor = floor_by_id.get(floor_id) if floor_id else None
            floor_name = str(floor.get("name")) if floor and floor.get("name") else None

            device_name = None
            if dev:
                raw_dev_name = dev.get("name_by_user") or dev.get("name")
                device_name = str(raw_dev_name) if raw_dev_name else None

            name = str(reg.get("name")) if reg.get("name") else None
            orig_name = str(reg.get("original_name")) if reg.get("original_name") else None
            friendly_name = str(
                attrs.get("friendly_name")
                or name
                or orig_name
                or (f"{device_name}" if device_name else eid.split(".", 1)[-1].replace("_", " "))
            )

            aliases_raw: list[str] = []
            for a_val in reg.get("aliases") or []:
                if isinstance(a_val, str) and a_val.strip():
                    aliases_raw.append(a_val.strip())
            if area:
                for a_val in area.get("aliases") or []:
                    if isinstance(a_val, str) and a_val.strip():
                        aliases_raw.append(a_val.strip())

            disabled = reg.get("disabled_by") is not None
            state_val = str(st.get("state")) if st.get("state") is not None else None

            object_part = eid.split(".", 1)[-1].replace("_", " ")
            search_chunks = [
                friendly_name,
                name or "",
                orig_name or "",
                device_name or "",
                area_name or "",
                floor_name or "",
                object_part,
                *aliases_raw,
            ]
            combined_norm = normalize_tr(" ".join(chunk for chunk in search_chunks if chunk))
            _, search_stems = stem_tr_phrase(combined_norm, strip_stopwords=False)
            _, area_stems = stem_tr_phrase(area_name or "", strip_stopwords=False)

            records[eid] = RegistryEntityRecord(
                entity_id=eid,
                domain=domain,
                name=name,
                original_name=orig_name,
                friendly_name=friendly_name,
                area_id=area_id,
                area_name=area_name,
                floor_id=floor_id,
                floor_name=floor_name,
                device_id=device_id,
                device_name=device_name,
                aliases=tuple(dict.fromkeys(aliases_raw)),
                state=state_val,
                disabled=disabled,
                search_text_norm=combined_norm,
                search_stems=search_stems,
                area_stems=area_stems,
            )
        return records

    # ------------------------------------------------------------------
    # Entity Memory SQLite operations (CoALA Semantic Memory)
    # ------------------------------------------------------------------

    async def _embed_phrase(self, text: str) -> tuple[list[float], str]:
        if self._embedder is not None:
            try:
                vec = await self._embedder.embed_text(text)
                return vec, self._embedder.model_name
            except Exception as exc:
                logger.warning("entity_resolver | embedder fallback triggered: %s", exc)
        return self._fallback_embedder.embed_sync(text), self._fallback_embedder.model_name

    def _upsert_memory_sync(
        self,
        phrase: str,
        phrase_norm: str,
        phrase_stems: str,
        emb_model: str,
        emb_json: str,
        entity_id: str,
        domain: str,
        area_id: str | None,
        source: str,
    ) -> EntityMemoryRecord:
        now = _utc_now_iso()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, source, success_count, failure_count
                FROM entity_memory
                WHERE phrase_norm = ? AND entity_id = ?
                """,
                (phrase_norm, entity_id),
            ).fetchone()
            if row is not None:
                row_id = int(row[0])
                eff_source = source if source == "user_correction" else str(row[1])
                succ = int(row[2]) + 1
                fail = int(row[3])
                conn.execute(
                    """
                    UPDATE entity_memory
                    SET phrase = ?, phrase_stems = ?, embedding_model = ?, embedding_json = ?,
                        domain = ?, area_id = ?, source = ?, success_count = ?, status = 'active',
                        last_verified_at = ?
                    WHERE id = ?
                    """,
                    (
                        phrase.strip(),
                        phrase_stems,
                        emb_model,
                        emb_json,
                        domain,
                        area_id,
                        eff_source,
                        succ,
                        now,
                        row_id,
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO entity_memory (
                        phrase, phrase_norm, phrase_stems, embedding_model, embedding_json,
                        entity_id, domain, area_id, source, success_count, failure_count,
                        status, created_at, last_verified_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 'active', ?, ?)
                    """,
                    (
                        phrase.strip(),
                        phrase_norm,
                        phrase_stems,
                        emb_model,
                        emb_json,
                        entity_id,
                        domain,
                        area_id,
                        source,
                        now,
                        now,
                    ),
                )
                row_id = int(cur.lastrowid or 0)
                succ = 1
                fail = 0
                eff_source = source
            self._rebuild_exact_alias_cache_from_conn(conn)

        return EntityMemoryRecord(
            id=row_id,
            phrase=phrase.strip(),
            phrase_norm=phrase_norm,
            phrase_stems=phrase_stems,
            entity_id=entity_id,
            domain=domain,
            area_id=area_id,
            source=eff_source,
            success_count=succ,
            failure_count=fail,
            confidence=_beta_bernoulli_confidence(eff_source, succ, fail),
            status="active",
            last_verified_at=now,
        )

    def _rebuild_exact_alias_cache_from_conn(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT phrase_norm, entity_id, source, success_count, failure_count
            FROM entity_memory
            WHERE status = 'active'
            """
        ).fetchall()
        by_phrase: dict[str, list[tuple[str, float]]] = {}
        for p_norm, eid, src, succ, fail in rows:
            conf = _beta_bernoulli_confidence(str(src), int(succ), int(fail))
            if conf >= 0.60:
                by_phrase.setdefault(str(p_norm), []).append((str(eid), conf))
        rebuilt: dict[str, str] = {}
        for p_norm, entries in by_phrase.items():
            if len(entries) == 1:
                rebuilt[p_norm] = entries[0][0]
        self._exact_memory_aliases = rebuilt

    def _rebuild_exact_alias_cache_sync(self) -> None:
        with self._connect() as conn:
            self._rebuild_exact_alias_cache_from_conn(conn)

    def _reject_memory_sync(self, phrase_norm: str, wrong_entity_id: str | None) -> int:
        now = _utc_now_iso()
        with self._connect() as conn:
            if wrong_entity_id:
                cur = conn.execute(
                    """
                    UPDATE entity_memory
                    SET failure_count = failure_count + 1, status = 'rejected', last_verified_at = ?
                    WHERE phrase_norm = ? AND entity_id = ?
                    """,
                    (now, phrase_norm, wrong_entity_id),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE entity_memory
                    SET failure_count = failure_count + 1, status = 'rejected', last_verified_at = ?
                    WHERE phrase_norm = ? AND status = 'active'
                    """,
                    (now, phrase_norm),
                )
            self._rebuild_exact_alias_cache_from_conn(conn)
            return int(cur.rowcount)

    def _mark_entity_stale_sync(self, entity_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE entity_memory SET status = 'stale' WHERE entity_id = ? AND status = 'active'",
                (entity_id,),
            )
            self._rebuild_exact_alias_cache_from_conn(conn)
            return int(cur.rowcount)

    def _migrate_entity_id_sync(self, old_entity_id: str, new_entity_id: str) -> None:
        new_domain = _extract_domain(new_entity_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, phrase_norm FROM entity_memory WHERE entity_id = ?",
                (old_entity_id,),
            ).fetchall()
            for row_id, phrase_norm in rows:
                conflict = conn.execute(
                    "SELECT id FROM entity_memory WHERE phrase_norm = ? AND entity_id = ?",
                    (phrase_norm, new_entity_id),
                ).fetchone()
                if conflict is not None:
                    conn.execute("DELETE FROM entity_memory WHERE id = ?", (row_id,))
                else:
                    conn.execute(
                        "UPDATE entity_memory SET entity_id = ?, domain = ?, status = 'active' WHERE id = ?",
                        (new_entity_id, new_domain, row_id),
                    )
            self._rebuild_exact_alias_cache_from_conn(conn)

    def _reconcile_memory_with_records_sync(self, records: Mapping[str, RegistryEntityRecord]) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, entity_id, area_id, status, source FROM entity_memory"
            ).fetchall()
            for row_id, eid, mem_area, status, source in rows:
                rec = records.get(str(eid))
                if rec is None:
                    if status == "active" and source != "static_alias":
                        conn.execute(
                            "UPDATE entity_memory SET status = 'stale' WHERE id = ?",
                            (row_id,),
                        )
                else:
                    updates: dict[str, Any] = {}
                    if status == "stale":
                        updates["status"] = "active"
                    if rec.area_id != mem_area:
                        updates["area_id"] = rec.area_id
                    if updates:
                        set_clause = ", ".join(f"{k} = ?" for k in updates)
                        conn.execute(
                            f"UPDATE entity_memory SET {set_clause} WHERE id = ?",
                            (*updates.values(), row_id),
                        )
            self._rebuild_exact_alias_cache_from_conn(conn)

    def _fetch_memory_rows_sync(
        self,
        status: str | None = "active",
    ) -> list[tuple[int, str, str, str, str, str, str, str, str | None, str, int, int, str, str]]:
        with self._connect() as conn:
            if status:
                return conn.execute(
                    """
                    SELECT id, phrase, phrase_norm, phrase_stems, embedding_model, embedding_json,
                           entity_id, domain, area_id, source, success_count, failure_count,
                           status, last_verified_at
                    FROM entity_memory
                    WHERE status = ?
                    ORDER BY last_verified_at DESC
                    """,
                    (status,),
                ).fetchall()
            return conn.execute(
                """
                SELECT id, phrase, phrase_norm, phrase_stems, embedding_model, embedding_json,
                       entity_id, domain, area_id, source, success_count, failure_count,
                       status, last_verified_at
                FROM entity_memory
                ORDER BY last_verified_at DESC
                """
            ).fetchall()

    async def list_memory(self, status: str | None = None) -> list[EntityMemoryRecord]:
        """List stored CoALA semantic entity mappings."""
        rows = await asyncio.to_thread(self._fetch_memory_rows_sync, status)
        return [
            EntityMemoryRecord(
                id=int(r[0]),
                phrase=str(r[1]),
                phrase_norm=str(r[2]),
                phrase_stems=str(r[3]),
                entity_id=str(r[6]),
                domain=str(r[7]),
                area_id=str(r[8]) if r[8] else None,
                source=str(r[9]),
                success_count=int(r[10]),
                failure_count=int(r[11]),
                confidence=_beta_bernoulli_confidence(str(r[9]), int(r[10]), int(r[11])),
                status=str(r[12]),
                last_verified_at=str(r[13]),
            )
            for r in rows
        ]

    async def record_verified_resolution(
        self,
        phrase: str,
        entity_id: str,
        source: str = "verified_action",
    ) -> EntityMemoryRecord | None:
        """Persist a phrase -> entity_id mapping ONLY after physical verification or explicit teaching."""
        cleaned_phrase = phrase.strip()
        canonical = entity_id.strip()
        if not cleaned_phrase or "." not in canonical:
            return None

        phrase_norm = normalize_tr(cleaned_phrase)
        phrase_stems_str, _ = stem_tr_phrase(cleaned_phrase)
        if not phrase_norm:
            return None

        # Never learn aliases to entities known not to exist when registry is populated
        if self._records and canonical not in self._records:
            logger.debug("entity_resolver | skip learning '%s' -> unknown entity '%s'", phrase, canonical)
            return None

        rec = self._records.get(canonical)
        area_id = rec.area_id if rec else None
        domain = rec.domain if rec else _extract_domain(canonical)
        emb_vec, emb_model = await self._embed_phrase(phrase_stems_str or phrase_norm)
        emb_json = json.dumps(emb_vec)

        async with self._write_lock:
            saved = await asyncio.to_thread(
                self._upsert_memory_sync,
                cleaned_phrase,
                phrase_norm,
                phrase_stems_str,
                emb_model,
                emb_json,
                canonical,
                domain,
                area_id,
                source,
            )
        logger.info(
            "entity_resolver | learned memory '%s' -> '%s' (confidence=%.2f, source=%s)",
            saved.phrase,
            saved.entity_id,
            saved.confidence,
            saved.source,
        )
        return saved

    async def record_user_correction(
        self,
        phrase: str,
        correct_entity_id: str,
        wrong_entity_id: str | None = None,
    ) -> EntityMemoryRecord | None:
        """Record negative evidence on wrong mapping(s) and learn the corrected entity."""
        phrase_norm = normalize_tr(phrase)
        if phrase_norm:
            async with self._write_lock:
                if wrong_entity_id and wrong_entity_id != correct_entity_id:
                    await asyncio.to_thread(self._reject_memory_sync, phrase_norm, wrong_entity_id)
                else:
                    # Reject other active rows for this exact phrase
                    rows = await asyncio.to_thread(self._fetch_memory_rows_sync, "active")
                    for row in rows:
                        if str(row[2]) == phrase_norm and str(row[6]) != correct_entity_id:
                            await asyncio.to_thread(self._reject_memory_sync, phrase_norm, str(row[6]))
        return await self.record_verified_resolution(
            phrase=phrase,
            entity_id=correct_entity_id,
            source="user_correction",
        )

    def on_entity_not_found(self, entity_id: str) -> None:
        """Layer 4 self-healing: mark stale and schedule immediate refresh if HA returns 404."""
        if not entity_id:
            return
        self._records.pop(entity_id, None)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._handle_404_async(entity_id))
        except RuntimeError:
            self._mark_entity_stale_sync(entity_id)

    async def _handle_404_async(self, entity_id: str) -> None:
        async with self._write_lock:
            changed = await asyncio.to_thread(self._mark_entity_stale_sync, entity_id)
        if changed:
            logger.warning("entity_resolver | 404 on '%s' -> marked %d memory row(s) stale", entity_id, changed)
        self._schedule_debounced_refresh(delay_sec=0.2)

    # ------------------------------------------------------------------
    # Synchronous alias helper + Verified Turn Phrase Tracker
    # ------------------------------------------------------------------

    def resolve_cached_alias(self, entity_or_alias: str) -> str | None:
        """Fast synchronous lookup used by ``HomeAssistantWSAdapter.resolve_entity_id``."""
        cleaned = entity_or_alias.strip()
        if not cleaned:
            return None
        low = cleaned.lower()
        if low in self._raw_aliases:
            return self._raw_aliases[low]
        norm_preserve = normalize_tr(cleaned, preserve_entity_chars=True)
        if norm_preserve in self._static_aliases:
            return self._static_aliases[norm_preserve]
        norm_phrase = normalize_tr(cleaned)
        if norm_phrase in self._exact_memory_aliases:
            return self._exact_memory_aliases[norm_phrase]
        return None

    def register_pending_resolution(self, query: str, result: EntityResolutionResult) -> None:
        """Track recently resolved candidates so a successful ``assert_state`` teaches memory."""
        now = time.monotonic()
        # Prune expired entries
        expired = [k for k, (_, ts) in self._recent_resolutions.items() if now - ts > _PENDING_QUERY_TTL_SEC]
        for k in expired:
            self._recent_resolutions.pop(k, None)

        for cand in result.candidates[:3]:
            if cand.confidence >= 0.55 and cand.match_source != "static_alias":
                self._recent_resolutions[cand.entity_id] = (query, now)

    def pop_pending_phrase(self, entity_id: str) -> str | None:
        """Return and consume the recent query associated with ``entity_id`` within TTL."""
        entry = self._recent_resolutions.pop(entity_id, None)
        if entry is None:
            return None
        phrase, ts = entry
        if time.monotonic() - ts > _PENDING_QUERY_TTL_SEC:
            return None
        return phrase

    # ------------------------------------------------------------------
    # Multi-Stage Entity Resolution Pipeline
    # ------------------------------------------------------------------

    def seed_records_for_test(self, records: list[RegistryEntityRecord]) -> None:
        """Inject synthetic registry records directly (intended for unit tests)."""
        self._records = {r.entity_id: r for r in records}

    @property
    def registry_size(self) -> int:
        return len(self._records)

    @property
    def last_sync_utc(self) -> str | None:
        return self._last_sync_utc

    def _upsert_candidate(
        self,
        best_by_entity: dict[str, EntityCandidate],
        candidate: EntityCandidate,
    ) -> None:
        existing = best_by_entity.get(candidate.entity_id)
        if existing is None or candidate.confidence > existing.confidence:
            best_by_entity[candidate.entity_id] = candidate

    async def resolve(
        self,
        query: str,
        domain_hint: str | None = None,
        limit: int = 5,
    ) -> EntityResolutionResult:
        """Resolve a natural-language Turkish/English entity reference in <10ms when cached."""
        started = time.perf_counter()
        raw_query = query.strip()
        query_norm = normalize_tr(raw_query)
        query_stems_str, query_stems = stem_tr_phrase(raw_query)

        explicit_domain = domain_hint.strip().lower() if domain_hint and domain_hint.strip() else None
        inferred = list(infer_domain_hints(raw_query))
        if explicit_domain and explicit_domain not in inferred:
            inferred.insert(0, explicit_domain)

        best_by_entity: dict[str, EntityCandidate] = {}

        # Stage 0a: Direct canonical entity_id passed as query
        eid_candidate_key = raw_query.lower()
        if "." in eid_candidate_key and eid_candidate_key in self._records:
            direct_rec = self._records[eid_candidate_key]
            self._upsert_candidate(
                best_by_entity,
                EntityCandidate(
                    entity_id=direct_rec.entity_id,
                    domain=direct_rec.domain,
                    friendly_name=direct_rec.friendly_name,
                    area_id=direct_rec.area_id,
                    area_name=direct_rec.area_name,
                    state=direct_rec.state,
                    confidence=0.99,
                    match_source="registry_grounding",
                    reason="Exact canonical entity_id match.",
                ),
            )

        # Stage 0b: Configured static YAML aliases
        static_target = self._raw_aliases.get(eid_candidate_key) or self._static_aliases.get(
            normalize_tr(raw_query, preserve_entity_chars=True)
        )
        if static_target:
            alias_rec = self._records.get(static_target)
            self._upsert_candidate(
                best_by_entity,
                EntityCandidate(
                    entity_id=static_target,
                    domain=_extract_domain(static_target),
                    friendly_name=alias_rec.friendly_name if alias_rec else static_target,
                    area_id=alias_rec.area_id if alias_rec else None,
                    area_name=alias_rec.area_name if alias_rec else None,
                    state=alias_rec.state if alias_rec else None,
                    confidence=0.99,
                    match_source="static_alias",
                    reason="Matched static entity_aliases config.",
                ),
            )

        # Stage 1 & 2: Exact and Fuzzy CoALA entity_memory
        memory_rows = await asyncio.to_thread(self._fetch_memory_rows_sync, "active")
        for row in memory_rows:
            mem_phrase = str(row[1])
            mem_norm = str(row[2])
            mem_stems_str = str(row[3])
            mem_eid = str(row[6])
            mem_domain = str(row[7])
            mem_source = str(row[9])
            mem_succ = int(row[10])
            mem_fail = int(row[11])

            if not _domains_compatible(explicit_domain, mem_domain):
                continue

            mem_conf = _beta_bernoulli_confidence(mem_source, mem_succ, mem_fail)
            mem_stems = tuple(mem_stems_str.split()) if mem_stems_str else ()

            mem_rec = self._records.get(mem_eid)
            friendly = mem_rec.friendly_name if mem_rec else mem_eid
            area_id = mem_rec.area_id if mem_rec else (str(row[8]) if row[8] else None)
            area_name = mem_rec.area_name if mem_rec else None
            state = mem_rec.state if mem_rec else None

            if query_norm == mem_norm or (query_stems_str and query_stems_str == mem_stems_str):
                # Clean verified memory outranks heuristic registry grounding (max 0.96)
                # while keeping explicit static overrides at 0.99 and failing memory penalized.
                if mem_fail == 0 and mem_conf >= 0.60:
                    score = min(0.985, 0.92 + 0.07 * mem_conf)
                else:
                    score = min(0.985, 0.60 + 0.40 * mem_conf)
                self._upsert_candidate(
                    best_by_entity,
                    EntityCandidate(
                        entity_id=mem_eid,
                        domain=mem_domain,
                        friendly_name=friendly,
                        area_id=area_id,
                        area_name=area_name,
                        state=state,
                        confidence=round(score, 4),
                        match_source="entity_memory_exact",
                        reason=f"Exact entity_memory hit ('{mem_phrase}', succ={mem_succ}, fail={mem_fail}).",
                    ),
                )
            else:
                f_sim = fuzzy_phrase_similarity(query_norm, query_stems, mem_norm, mem_stems)
                if f_sim >= 0.45:
                    score = min(0.95, f_sim * (0.50 + 0.50 * mem_conf))
                    self._upsert_candidate(
                        best_by_entity,
                        EntityCandidate(
                            entity_id=mem_eid,
                            domain=mem_domain,
                            friendly_name=friendly,
                            area_id=area_id,
                            area_name=area_name,
                            state=state,
                            confidence=round(score, 4),
                            match_source="entity_memory_fuzzy",
                            reason=f"Fuzzy entity_memory match ('{mem_phrase}', sim={f_sim:.2f}).",
                        ),
                    )

        # Stage 3: Grounding Index deterministic scoring across HA registry (<15ms local)
        for rec in self._records.values():
            if rec.disabled:
                continue
            if explicit_domain not in _IGNORED_NOISE_DOMAINS and rec.domain in _IGNORED_NOISE_DOMAINS:
                continue
            cand = self._score_registry_record(
                rec=rec,
                query_norm=query_norm,
                query_stems=query_stems,
                explicit_domain=explicit_domain,
                inferred_domains=inferred,
            )
            if cand is not None:
                self._upsert_candidate(best_by_entity, cand)

        # Stage 4: Semantic embedding similarity on entity_memory (network fallback ONLY if unresolved)
        best_score_so_far = max((c.confidence for c in best_by_entity.values()), default=0.0)
        if (
            self._embedder is not None
            and memory_rows
            and best_score_so_far < self._confidence_threshold
            and query_norm
        ):
            try:
                q_vec = await self._embedder.embed_text(query_stems_str or query_norm)
                q_model = self._embedder.model_name
            except Exception:
                q_vec = self._fallback_embedder.embed_sync(query_stems_str or query_norm)
                q_model = self._fallback_embedder.model_name

            for row in memory_rows:
                emb_model = str(row[4])
                emb_json = str(row[5])
                if emb_model != q_model or not emb_json:
                    continue
                mem_eid = str(row[6])
                mem_domain = str(row[7])
                if not _domains_compatible(explicit_domain, mem_domain):
                    continue
                try:
                    m_vec = [float(x) for x in json.loads(emb_json)]
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
                cos = _cosine_similarity(q_vec, m_vec)
                if cos >= 0.60:
                    mem_conf = _beta_bernoulli_confidence(str(row[9]), int(row[10]), int(row[11]))
                    score = min(0.95, cos * (0.50 + 0.50 * mem_conf))
                    sem_rec = self._records.get(mem_eid)
                    self._upsert_candidate(
                        best_by_entity,
                        EntityCandidate(
                            entity_id=mem_eid,
                            domain=mem_domain,
                            friendly_name=sem_rec.friendly_name if sem_rec else mem_eid,
                            area_id=sem_rec.area_id if sem_rec else (str(row[8]) if row[8] else None),
                            area_name=sem_rec.area_name if sem_rec else None,
                            state=sem_rec.state if sem_rec else None,
                            confidence=round(score, 4),
                            match_source="entity_memory_semantic",
                            reason=f"Semantic entity_memory match ('{row[1]}', cos={cos:.2f}).",
                        ),
                    )

        ranked = sorted(best_by_entity.values(), key=lambda c: c.confidence, reverse=True)[: max(1, limit)]
        ambiguous = False
        if len(ranked) >= 2:
            c1, c2 = ranked[0], ranked[1]
            if c1.confidence >= 0.70 and c2.confidence >= 0.70 and (c1.confidence - c2.confidence) <= 0.06:
                ambiguous = True

        top = ranked[0] if ranked else None
        resolved = bool(top is not None and top.confidence >= self._confidence_threshold and not ambiguous)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)

        result = EntityResolutionResult(
            query=raw_query,
            normalized_query=query_norm,
            inferred_domains=inferred,
            resolved=resolved,
            ambiguous=ambiguous,
            top_entity_id=top.entity_id if top else None,
            candidates=ranked,
            elapsed_ms=elapsed_ms,
        )
        self.register_pending_resolution(raw_query, result)
        return result

    @staticmethod
    def _score_registry_record(
        rec: RegistryEntityRecord,
        query_norm: str,
        query_stems: tuple[str, ...],
        explicit_domain: str | None,
        inferred_domains: list[str],
    ) -> EntityCandidate | None:
        # Exact HA alias match
        for alias in rec.aliases:
            alias_norm = normalize_tr(alias)
            _, alias_stems = stem_tr_phrase(alias, strip_stopwords=False)
            if query_norm == alias_norm or (query_stems and query_stems == alias_stems):
                return EntityCandidate(
                    entity_id=rec.entity_id,
                    domain=rec.domain,
                    friendly_name=rec.friendly_name,
                    area_id=rec.area_id,
                    area_name=rec.area_name,
                    state=rec.state,
                    confidence=0.95,
                    match_source="registry_grounding",
                    reason=f"Exact HA alias '{alias}' match.",
                )

        if not query_stems or not rec.search_stems:
            return None

        q_set = set(query_stems)
        base_rec_set = set(rec.search_stems)
        if q_set & base_rec_set:
            rec_set = base_rec_set | _DOMAIN_IMPLICIT_STEMS.get(rec.domain, frozenset())
        else:
            rec_set = base_rec_set
        matched_stems = q_set & rec_set

        friendly_norm: str
        friendly_stems: tuple[str, ...]
        if not matched_stems:
            # Fast-reject records with zero lexical overlap before running expensive stem/fuzz checks
            if len(query_norm) < 4 or query_norm[:4] not in rec.search_text_norm:
                return None
            # Fallback character fuzzy similarity on friendly_name only
            friendly_norm = normalize_tr(rec.friendly_name)
            _, friendly_stems = stem_tr_phrase(rec.friendly_name, strip_stopwords=False)
            f_sim = fuzzy_phrase_similarity(query_norm, query_stems, friendly_norm, friendly_stems)
            if f_sim < 0.55:
                return None
            base_score = 0.50 * f_sim
            coverage = 0.0
        else:
            coverage = len(matched_stems) / len(q_set)
            friendly_norm = normalize_tr(rec.friendly_name)
            _, friendly_stems = stem_tr_phrase(rec.friendly_name, strip_stopwords=False)
            f_sim = fuzzy_phrase_similarity(query_norm, query_stems, friendly_norm, friendly_stems)
            base_score = 0.52 * coverage + 0.24 * f_sim

        area_bonus = 0.0
        if rec.area_stems and (q_set & set(rec.area_stems)):
            area_bonus = 0.14

        domain_adj = 0.0
        if explicit_domain:
            if rec.domain == explicit_domain:
                domain_adj = 0.16
            elif _domains_compatible(explicit_domain, rec.domain):
                domain_adj = 0.12
            else:
                domain_adj = -0.25
        elif inferred_domains:
            if rec.domain in inferred_domains:
                # Primary inferred domain gets slightly higher priority than secondary (e.g. light vs switch)
                idx = inferred_domains.index(rec.domain)
                domain_adj = 0.15 if idx == 0 else 0.12
            elif rec.domain in {"sensor", "binary_sensor", "number", "select", "button"}:
                domain_adj = -0.15

        # Slight tie-break penalty if friendly_name has many unmatched extra stems
        extra_penalty = 0.0
        if friendly_stems and len(set(friendly_stems) - q_set) > 2:
            extra_penalty = 0.04

        total = min(0.96, max(0.0, base_score + area_bonus + domain_adj - extra_penalty))
        if total < 0.35:
            return None

        return EntityCandidate(
            entity_id=rec.entity_id,
            domain=rec.domain,
            friendly_name=rec.friendly_name,
            area_id=rec.area_id,
            area_name=rec.area_name,
            state=rec.state,
            confidence=round(total, 4),
            match_source="registry_grounding",
            reason=(
                f"Registry grounding (stems={sorted(matched_stems)}, "
                f"cov={coverage:.2f}, area_bonus={area_bonus:.2f}, dom_adj={domain_adj:+.2f})."
            ),
        )
