"""Abstract memory interfaces (Dependency Inversion).

Agents depend on :class:`BaseMemoryStore` / :class:`BaseEmbedder` only, so the
SQLite implementation can be swapped (or mocked in tests) without touching agents.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel


class BaseEmbedder(ABC):
    """Turns text into a dense vector."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Stable identifier; vectors from different models are never compared."""

    @abstractmethod
    async def embed_text(self, text: str) -> list[float]:
        """Return the embedding of ``text``."""


class LessonRecord(BaseModel):
    """A persisted lesson learned."""

    id: int
    domain: str
    rule_content: str
    is_core_invariant: bool
    occurrence_count: int = 1
    active: bool = True
    created_at: datetime | None = None
    created: bool = True


@dataclass(frozen=True)
class RetrievedRules:
    """Result of the 3-tier retrieval."""

    core: list[str] = field(default_factory=list)
    semantic: list[str] = field(default_factory=list)
    degraded: bool = False

    def as_list(self) -> list[str]:
        """Core invariants first (tagged), then semantic matches, de-duplicated."""
        tagged = [f"[CORE INVARIANT] {rule}" for rule in self.core]
        return list(dict.fromkeys([*tagged, *self.semantic]))

    def render(self) -> str:
        """Prompt block injected into a sub-agent system prompt (empty when no rules)."""
        rules = self.as_list()
        if not rules:
            return ""
        lines = "\n".join(f"- {rule}" for rule in rules)
        return (
            "\n\n## Lessons learned (MANDATORY - these override your defaults)\n"
            f"{lines}\n"
        )


class BaseMemoryStore(ABC):
    """Long-term reflection memory with domain isolation."""

    @abstractmethod
    async def initialize(self) -> None:
        """Create schema / migrate / re-embed stale rows."""

    @abstractmethod
    async def retrieve(self, domain: str, query: str) -> RetrievedRules:
        """3-tier retrieval: domain filter -> core invariants -> semantic matches."""

    async def get_relevant_rules(self, domain: str, query: str) -> list[str]:
        """Flat list of relevant rules (core invariants tagged)."""
        return (await self.retrieve(domain, query)).as_list()

    @abstractmethod
    async def record_lesson(
        self,
        domain: str,
        rule_content: str,
        is_core_invariant: bool = False,
        min_occurrences: int = 1,
    ) -> LessonRecord:
        """Persist a lesson (idempotent on ``(domain, rule_content)``)."""

    @abstractmethod
    async def seed(self, lessons: Iterable[tuple[str, str, bool]]) -> int:
        """Insert ``(domain, rule, is_core)`` tuples idempotently; returns the number inserted."""

