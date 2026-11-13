"""3-Tier Zero-Miss Reflection Store (SQLite + Semantic Embeddings).

Tier 1 - Bounded-Context Isolation: filter by ``domain`` (``ha`` / ``infra``).
Tier 2 - Core Invariants: rows with ``is_core_invariant = 1`` are ALWAYS returned (0% miss).
Tier 3 - Semantic Vector Match: cosine similarity >= ``similarity_threshold`` using
         Google GenAI embeddings (with an ultra-lightweight hashed n-gram fallback so
         memory keeps working offline and under ~80 MB RAM).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types as genai_types
from strands import tool
from strands.types.tools import AgentTool

from src.core.config import AppConfig
from src.core.exceptions import GuardViolationError, MemoryPoisoningError
from src.memory.base import BaseEmbedder, BaseMemoryStore, LessonRecord, RetrievedRules

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)
_TRANSIENT_ERROR_RE = re.compile(
    r"\b("
    r"timeout|timed\s*out|time[- ]out|timeout_error|timeouterror|"
    r"connection\s*(refused|reset|error|aborted|closed|failed)|connectionerror|"
    r"circuit[_\s]*open|circuit\s*breaker|devre\s*kesici|"
    r"zaman\s*a[şs][ıi]m[ıi]|ba[ğg]lant[ıi]\s*hatas[ıi]|"
    r"502\s*bad\s*gateway|503\s*service\s*unavailable|504\s*gateway\s*timeout|"
    r"temporarily\s*unavailable|anl[ıi]k\s*hata|ge[çc]ici\s*hata"
    r")\b|"
    r"\b(unreachable|eri[şs]ilemiyor|ula[şs][ıi]lam[ıi]yor)\b.*\b(do\s*not|don't|never|bir\s*daha|tekrar|asla)\b|"
    r"\b(do\s*not|don't|never|bir\s*daha|tekrar|asla)\b.*\b(unreachable|eri[şs]ilemiyor|ula[şs][ıi]lam[ıi]yor)\b",
    re.IGNORECASE,
)
_NEAR_DUPLICATE_THRESHOLD = 0.88


def validate_lesson_content(rule_content: str) -> None:
    """Reject transient network/timeout/circuit-breaker failures to prevent memory drift."""
    match = _TRANSIENT_ERROR_RE.search(rule_content)
    if match:
        raise MemoryPoisoningError(
            f"Refused to persist transient/reachability error pattern '{match.group(0)}' into reflection store.",
            matched_pattern=match.group(0),
        )


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 1e-12:
        return vec
    return [v / norm for v in vec]


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=False))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    denom = norm_a * norm_b
    if denom <= 1e-12:
        return 0.0
    return float(dot / denom)


class LocalHashedNgramEmbedder(BaseEmbedder):
    """Zero-dependency hashed word + character 3-gram embedder (~0 MB RAM overhead).

    Used when ``memory.embedding_backend == "local"`` or as a graceful fallback if
    the remote embedding API is temporarily unreachable.
    """

    def __init__(self, dimensions: int) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be > 0")
        self._dimensions = dimensions

    @property
    def model_name(self) -> str:
        return f"local-ngram-{self._dimensions}"

    async def embed_text(self, text: str) -> list[float]:
        return self.embed_sync(text)

    def embed_sync(self, text: str) -> list[float]:
        vec = [0.0] * self._dimensions
        normalized = text.lower().strip()
        if not normalized:
            return vec
        tokens = _TOKEN_RE.findall(normalized)
        features: list[tuple[str, float]] = []
        for tok in tokens:
            features.append((f"w:{tok}", 2.0))
            padded = f"<{tok}>"
            for idx in range(max(0, len(padded) - 2)):
                features.append((f"c:{padded[idx : idx + 3]}", 1.0))
        for idx in range(len(tokens) - 1):
            features.append((f"b:{tokens[idx]}_{tokens[idx + 1]}", 1.5))

        for feat, weight in features:
            digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
            raw = int.from_bytes(digest, "little")
            bucket = raw % self._dimensions
            sign = 1.0 if ((raw >> 63) & 1) == 0 else -1.0
            vec[bucket] += sign * weight
        return _l2_normalize(vec)


class GeminiEmbedder(BaseEmbedder):
    """Google GenAI embedding adapter with automatic local fallback on API error."""

    def __init__(
        self,
        api_key: str,
        embedding_model_id: str,
        fallback_dimensions: int,
        client: genai.Client | None = None,
    ) -> None:
        self._model_id = embedding_model_id
        self._client = client or genai.Client(api_key=api_key)
        self._fallback = LocalHashedNgramEmbedder(dimensions=fallback_dimensions)

    @property
    def model_name(self) -> str:
        return f"gemini:{self._model_id}"

    @property
    def fallback(self) -> LocalHashedNgramEmbedder:
        return self._fallback

    async def embed_text(self, text: str) -> list[float]:
        response = await self._client.aio.models.embed_content(
            model=self._model_id,
            contents=text,
            config=genai_types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT"),
        )
        embeddings = response.embeddings or []
        if not embeddings or not embeddings[0].values:
            raise RuntimeError("Empty embedding response from Gemini API.")
        return _l2_normalize([float(x) for x in embeddings[0].values])


def build_embedder(config: AppConfig, client: genai.Client | None = None) -> BaseEmbedder:
    """Construct the configured embedder (DIP factory)."""
    if config.memory.embedding_backend == "local":
        return LocalHashedNgramEmbedder(dimensions=config.memory.local_embedding_dimensions)
    return GeminiEmbedder(
        api_key=config.gemini_api_key.get_secret_value(),
        embedding_model_id=config.llm.embedding_model_id,
        fallback_dimensions=config.memory.local_embedding_dimensions,
        client=client,
    )


class ReflectionStore(BaseMemoryStore):
    """3-Tier SQLite + Semantic Embedding store."""

    def __init__(
        self,
        db_path: str | Path,
        embedder: BaseEmbedder,
        similarity_threshold: float,
        max_rules_injected: int = 12,
        local_fallback_dimensions: int = 256,
    ) -> None:
        self.db_path = Path(db_path)
        self.embedder = embedder
        self.similarity_threshold = similarity_threshold
        self.max_rules_injected = max_rules_injected
        self._fallback_embedder = LocalHashedNgramEmbedder(dimensions=local_fallback_dimensions)
        self._write_lock = asyncio.Lock()
        self._db_initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _ensure_db_sync(self) -> None:
        if not self._db_initialized:
            self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lessons_learned (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    is_core_invariant INTEGER NOT NULL DEFAULT 0,
                    rule_content TEXT NOT NULL,
                    embedding_model TEXT NOT NULL DEFAULT '',
                    embedding_json TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    min_occurrences INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(domain, rule_content)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_lessons_domain_core
                ON lessons_learned (domain, is_core_invariant)
                """
            )
            # Ensure backward compatibility with pre-existing schemas
            cols = {row[1] for row in conn.execute("PRAGMA table_info(lessons_learned)").fetchall()}
            if "embedding_model" not in cols:
                conn.execute("ALTER TABLE lessons_learned ADD COLUMN embedding_model TEXT NOT NULL DEFAULT ''")
            if "occurrence_count" not in cols:
                conn.execute("ALTER TABLE lessons_learned ADD COLUMN occurrence_count INTEGER NOT NULL DEFAULT 1")
            if "min_occurrences" not in cols:
                conn.execute("ALTER TABLE lessons_learned ADD COLUMN min_occurrences INTEGER NOT NULL DEFAULT 1")
        self._db_initialized = True

    async def initialize(self) -> None:
        """Verify schema and enable WAL mode asynchronously."""
        async with self._write_lock:
            await asyncio.to_thread(self._init_db)

    async def _embed_with_fallback(self, text: str) -> tuple[list[float], str, bool]:
        try:
            vec = await self.embedder.embed_text(text)
            return vec, self.embedder.model_name, False
        except Exception as exc:
            logger.warning(
                "embedder=<%s> error=<%s> | falling back to local n-gram embedder",
                self.embedder.model_name,
                exc,
            )
            vec = self._fallback_embedder.embed_sync(text)
            return vec, self._fallback_embedder.model_name, True

    async def retrieve(self, domain: str, query: str) -> RetrievedRules:
        """Execute 3-Tier retrieval: Domain Filter -> Core Invariants -> Semantic Similarity."""
        norm_domain = domain.strip().lower()
        rows = await asyncio.to_thread(self._fetch_domain_rows, norm_domain)
        if not rows:
            return RetrievedRules()

        core_rules: list[str] = []
        non_core_rows: list[tuple[str, str, str]] = []
        for rule_content, is_core, emb_model, emb_json in rows:
            if int(is_core) == 1:
                core_rules.append(rule_content)
            else:
                non_core_rows.append((rule_content, emb_model, emb_json))

        if not non_core_rows or not query.strip():
            return RetrievedRules(core=core_rules, semantic=[], degraded=False)

        query_vec, query_model, degraded = await self._embed_with_fallback(query)
        semantic_rules = await asyncio.to_thread(
            self._score_semantic_rows_sync,
            non_core_rows,
            query_vec,
            query_model,
            len(core_rules),
        )
        return RetrievedRules(core=core_rules, semantic=semantic_rules, degraded=degraded)

    def _score_semantic_rows_sync(
        self,
        non_core_rows: list[tuple[str, str, str]],
        query_vec: list[float],
        query_model: str,
        core_count: int,
    ) -> list[str]:
        scored: list[tuple[float, str]] = []
        for rule_content, emb_model, emb_json in non_core_rows:
            rule_vec = self._resolve_rule_vector(
                rule_content, emb_model, emb_json, query_model, len(query_vec)
            )
            sim = _cosine_similarity(query_vec, rule_vec)
            if sim >= self.similarity_threshold:
                scored.append((sim, rule_content))

        scored.sort(key=lambda item: item[0], reverse=True)
        budget = max(0, self.max_rules_injected - core_count)
        return [content for _, content in scored[:budget]]

    def _resolve_rule_vector(
        self,
        rule_content: str,
        emb_model: str,
        emb_json: str,
        target_model: str,
        expected_dim: int,
    ) -> list[float]:
        if emb_model == target_model or not emb_model:
            try:
                parsed = [float(x) for x in json.loads(emb_json)]
                if len(parsed) == expected_dim:
                    return parsed
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        # Dimension / model mismatch (e.g. degraded mode): compute local vector on the fly
        if target_model == self._fallback_embedder.model_name:
            return self._fallback_embedder.embed_sync(rule_content)
        try:
            parsed = [float(x) for x in json.loads(emb_json)]
            if len(parsed) == expected_dim:
                return parsed
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        return []

    def _fetch_domain_rows(self, domain: str) -> list[tuple[str, int, str, str]]:
        self._ensure_db_sync()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT rule_content, is_core_invariant, embedding_model, embedding_json
                FROM lessons_learned
                WHERE domain = ?
                  AND (is_core_invariant = 1 OR occurrence_count >= min_occurrences)
                ORDER BY id ASC
                """,
                (domain,),
            )
            return [(str(r[0]), int(r[1]), str(r[2]), str(r[3])) for r in cursor.fetchall()]

    async def get_relevant_rules(self, domain: str, query: str) -> list[str]:
        """Blueprint-compatible helper returning ``[CORE INVARIANT] ...`` + semantic matches."""
        result = await self.retrieve(domain, query)
        return result.as_list()

    async def record_lesson(
        self,
        domain: str,
        rule_content: str,
        is_core_invariant: bool = False,
        min_occurrences: int = 1,
    ) -> LessonRecord:
        """Persist a lesson learned idempotently with anti-poisoning and occurrence gating."""
        norm_domain = domain.strip().lower()
        clean_rule = rule_content.strip()
        if not norm_domain or not clean_rule:
            raise ValueError("Both domain and rule_content must be non-empty.")

        validate_lesson_content(clean_rule)

        effective_min = 1 if is_core_invariant else max(1, int(min_occurrences))
        vec, model_name, _ = await self._embed_with_fallback(clean_rule)
        emb_json = json.dumps(vec)
        async with self._write_lock:
            return await asyncio.to_thread(
                self._upsert_row,
                norm_domain,
                clean_rule,
                1 if is_core_invariant else 0,
                model_name,
                emb_json,
                vec,
                effective_min,
            )

    def _find_near_duplicate_row(
        self,
        conn: sqlite3.Connection,
        domain: str,
        model_name: str,
        query_vec: list[float],
    ) -> tuple[int, int, str, int, int, Any] | None:
        candidates = conn.execute(
            """
            SELECT id, is_core_invariant, rule_content, embedding_model, embedding_json,
                   occurrence_count, min_occurrences, created_at
            FROM lessons_learned
            WHERE domain = ? AND is_core_invariant = 0
            ORDER BY id ASC
            """,
            (domain,),
        ).fetchall()
        best_sim = 0.0
        best_row: tuple[int, int, str, int, int, Any] | None = None
        for row in candidates:
            cand_id = int(row[0])
            cand_core = int(row[1])
            cand_rule = str(row[2])
            cand_model = str(row[3])
            cand_json = str(row[4])
            cand_occ = int(row[5])
            cand_min = int(row[6])
            cand_created = row[7]
            cand_vec = self._resolve_rule_vector(
                cand_rule, cand_model, cand_json, model_name, len(query_vec)
            )
            sim = _cosine_similarity(query_vec, cand_vec)
            if sim >= _NEAR_DUPLICATE_THRESHOLD and sim > best_sim:
                best_sim = sim
                best_row = (cand_id, cand_core, cand_rule, cand_occ, cand_min, cand_created)
        return best_row

    def _upsert_row(
        self,
        domain: str,
        rule_content: str,
        is_core: int,
        model_name: str,
        emb_json: str,
        vec: list[float],
        effective_min: int,
    ) -> LessonRecord:
        self._ensure_db_sync()
        with self._connect() as conn:
            exact = conn.execute(
                """
                SELECT id, is_core_invariant, rule_content, occurrence_count, min_occurrences, created_at
                FROM lessons_learned
                WHERE domain = ? AND rule_content = ?
                """,
                (domain, rule_content),
            ).fetchone()
            existing: tuple[int, int, str, int, int, Any] | None
            if exact is not None:
                existing = (
                    int(exact[0]),
                    int(exact[1]),
                    str(exact[2]),
                    int(exact[3]),
                    int(exact[4]),
                    exact[5],
                )
            elif is_core == 0:
                existing = self._find_near_duplicate_row(conn, domain, model_name, vec)
            else:
                existing = None

            if existing is not None:
                row_id, existing_core, existing_rule, existing_count, existing_min, created_at = existing
                new_core = 1 if (existing_core == 1 or is_core == 1) else 0
                new_count = existing_count + 1
                new_min = 1 if new_core == 1 else min(existing_min, effective_min)
                conn.execute(
                    """
                    UPDATE lessons_learned
                    SET is_core_invariant = ?, occurrence_count = ?, min_occurrences = ?
                    WHERE id = ?
                    """,
                    (new_core, new_count, new_min, row_id),
                )
                return LessonRecord(
                    id=row_id,
                    domain=domain,
                    rule_content=existing_rule,
                    is_core_invariant=bool(new_core),
                    occurrence_count=new_count,
                    active=bool(new_core == 1 or new_count >= new_min),
                    created_at=created_at,
                    created=False,
                )

            cursor = conn.execute(
                """
                INSERT INTO lessons_learned (
                    domain, is_core_invariant, rule_content, embedding_model, embedding_json,
                    occurrence_count, min_occurrences
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (domain, is_core, rule_content, model_name, emb_json, 1, effective_min),
            )
            new_id = int(cursor.lastrowid or 0)
            return LessonRecord(
                id=new_id,
                domain=domain,
                rule_content=rule_content,
                is_core_invariant=bool(is_core),
                occurrence_count=1,
                active=bool(is_core == 1 or 1 >= effective_min),
                created=True,
            )

    async def seed(self, lessons: Iterable[tuple[str, str, bool]]) -> int:
        """Seed initial lessons idempotently; returns count of newly created lessons."""
        inserted = 0
        for domain, rule, is_core in lessons:
            rec = await self.record_lesson(domain=domain, rule_content=rule, is_core_invariant=is_core)
            if rec.created:
                inserted += 1
        return inserted


def create_record_lesson_tool(
    store: BaseMemoryStore,
    allowed_domains: Iterable[str],
    allow_core_invariants: bool = False,
    agent_min_occurrences: int = 2,
) -> AgentTool:
    """Build the Strands tool ``record_lesson_learned`` bound to ``store``."""
    valid_domains = {d.strip().lower() for d in allowed_domains}

    @tool(
        name="record_lesson_learned",
        description=(
            "Persist a self-healing lesson learned (entity mapping or logical parameter quirk) "
            "into the 3-tier reflection store. NEVER record transient network, timeout, or "
            "circuit-breaker errors (e.g. TimeoutError, ConnectionError, unreachable device)."
        ),
    )
    async def record_lesson_learned(
        domain: str,
        rule_content: str,
        is_core_invariant: bool = False,
    ) -> dict[str, Any]:
        """Record a domain-isolated lesson learned into SQLite reflection memory.

        Args:
            domain: Target bounded context (e.g. 'ha' or 'infra').
            rule_content: Concise, actionable rule to remember for future tasks.
            is_core_invariant: Whether this rule is a core invariant (always injected).
        """
        norm_domain = domain.strip().lower()
        if valid_domains and norm_domain not in valid_domains:
            raise GuardViolationError(
                f"Unknown reflection domain '{domain}'. Allowed domains: {sorted(valid_domains)}.",
                domain=domain,
            )
        promote_core = bool(is_core_invariant and allow_core_invariants)
        min_occ = 1 if promote_core else max(1, int(agent_min_occurrences))
        record = await store.record_lesson(
            domain=norm_domain,
            rule_content=rule_content,
            is_core_invariant=promote_core,
            min_occurrences=min_occ,
        )
        return {
            "id": record.id,
            "domain": record.domain,
            "rule_content": record.rule_content,
            "is_core_invariant": record.is_core_invariant,
            "occurrence_count": record.occurrence_count,
            "active": record.active,
            "created": record.created,
        }

    return record_lesson_learned

