"""HomePod / AirPlay TTS Announcer & Voice Text Normalization (`src/core/announcer.py`)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from src.core.config import AppConfig
from src.core.entity_resolver import EntityCandidate, EntityResolutionResult, EntityResolver
from src.core.events import ScheduleResultEvent
from src.core.exceptions import HomelabAgentError
from src.core.interfaces import BaseStateVerifier, EntityState
from src.core.text_tr import normalize_tr

logger = logging.getLogger(__name__)

_WORD_SPAN_RE = re.compile(r"[^\s,.:;!?\"'«»()]+")
_CODE_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_-]+\n)?(.*?)```", re.DOTALL)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_BULLET_RE = re.compile(r"(?m)^\s*(?:[-*•]+|\d+\.)\s+")
_EMPHASIS_RE = re.compile(r"(?<!\w)[*_]{1,3}|[*_]{1,3}(?!\w)")
_MULTI_SPACE_RE = re.compile(r"\s+")


class HATTSBackend(Protocol):
    """Subset of Home Assistant adapter required by :class:`Announcer`."""

    def resolve_entity_id(self, entity_or_alias: str) -> str:
        """Resolve configured or cached alias into a canonical ``entity_id``."""
        ...

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        service_data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a Home Assistant service call."""
        ...


class AnnouncementResult(BaseModel):
    """Result of a HomePod / AirPlay TTS announcement dispatch."""

    delivered: bool
    speaker_entity: str | None = None
    room: str | None = None
    message: str
    tts_service: str | None = None
    reason: str | None = None


def strip_wake_prefix(text: str, wake_words: Sequence[str]) -> tuple[str, bool]:
    """Strip a leading Turkish/English wake phrase while preserving the remaining text.

    Returns ``(cleaned_text, wake_word_detected)``. If stripping leaves an empty command,
    preserves the original trimmed input alongside ``wake_word_detected=True``.
    """
    raw = text.strip()
    if not raw or not wake_words:
        return raw, False

    spans = list(_WORD_SPAN_RE.finditer(raw))
    if not spans:
        return raw, False

    # Reject if leading punctuation is actually non-punctuation garbage before first token.
    leading_prefix = raw[: spans[0].start()].strip()
    if leading_prefix and any(ch.isalnum() for ch in leading_prefix):
        return raw, False

    sorted_wakes = sorted(
        (w for w in wake_words if normalize_tr(w)),
        key=lambda item: (-len(normalize_tr(item).split()), -len(item)),
    )

    norm_tokens = [normalize_tr(m.group(0)) for m in spans]
    for wake in sorted_wakes:
        w_tokens = normalize_tr(wake).split()
        if not w_tokens or len(norm_tokens) < len(w_tokens):
            continue
        if norm_tokens[: len(w_tokens)] == w_tokens:
            end_pos = spans[len(w_tokens) - 1].end()
            remainder = raw[end_pos:].lstrip(" \t\r\n,.:;!?-–—")
            return (remainder if remainder else raw), True

    return raw, False


def clean_for_tts(text: str) -> str:
    """Convert markdown-formatted agent output into clean, natural single-paragraph speech."""
    if not text:
        return ""
    cleaned = _CODE_FENCE_RE.sub(r" \1 ", text)
    cleaned = _MD_LINK_RE.sub(r"\1", cleaned)
    cleaned = _INLINE_CODE_RE.sub(r"\1", cleaned)
    cleaned = _HEADING_RE.sub("", cleaned)
    cleaned = _BULLET_RE.sub("", cleaned)
    cleaned = _EMPHASIS_RE.sub("", cleaned)
    cleaned = _MULTI_SPACE_RE.sub(" ", cleaned).strip()
    return cleaned


class Announcer:
    """Routes spoken responses and background completion alerts to HomePod speakers."""

    def __init__(
        self,
        config: AppConfig,
        ha_adapter: HATTSBackend,
        entity_resolver: EntityResolver | None = None,
    ) -> None:
        self._config = config
        self._ha = ha_adapter
        self._resolver = entity_resolver

    def _lookup_configured_room_speaker(self, room: str) -> str | None:
        room_norm = normalize_tr(room)
        if not room_norm:
            return None
        normalized_map: dict[str, str] = {}
        for key, target in self._config.voice.room_speakers.items():
            for variant in (normalize_tr(key), normalize_tr(key.replace("_", " "))):
                if variant:
                    normalized_map[variant] = target
        matched = normalized_map.get(room_norm) or normalized_map.get(normalize_tr(room.replace("_", " ")))
        return self._ha.resolve_entity_id(matched) if matched else None

    async def resolve_speaker(
        self,
        room: str | None = None,
        speaker_entity: str | None = None,
    ) -> str | None:
        """Resolve the target HomePod / `media_player.*` entity for an announcement."""
        if speaker_entity and speaker_entity.strip():
            return self._ha.resolve_entity_id(speaker_entity.strip())

        if room and room.strip():
            configured = self._lookup_configured_room_speaker(room.strip())
            if configured:
                return configured
            if self._resolver is not None:
                for query in (f"{room.strip()} homepod", room.strip()):
                    res = await self._resolver.resolve(query, domain_hint="media_player", limit=1)
                    if res.resolved and res.top_entity_id:
                        return res.top_entity_id

        default_spk = self._config.voice.default_speaker
        if default_spk and default_spk.strip():
            return self._ha.resolve_entity_id(default_spk.strip())
        return None

    async def announce(
        self,
        message: str,
        room: str | None = None,
        speaker_entity: str | None = None,
        language: str | None = None,
    ) -> AnnouncementResult:
        """Deliver a clean TTS announcement to the resolved HomePod speaker."""
        clean_msg = clean_for_tts(message)
        voice_cfg = self._config.voice
        if not voice_cfg.enabled or not clean_msg:
            return AnnouncementResult(
                delivered=False,
                room=room,
                message=clean_msg,
                reason="voice_disabled_or_empty",
            )

        target_speaker = await self.resolve_speaker(room=room, speaker_entity=speaker_entity)
        if target_speaker is None:
            return AnnouncementResult(
                delivered=False,
                room=room,
                message=clean_msg,
                reason="no_speaker_configured",
            )

        eff_lang = (language or voice_cfg.language).strip() or "tr"
        configured_service = voice_cfg.tts_service.strip() or "tts.speak"
        domain, sep, service = configured_service.partition(".")
        if not sep:
            domain, service = "tts", configured_service

        if f"{domain}.{service}" == "tts.speak":
            try:
                await self._ha.call_service(
                    domain="tts",
                    service="speak",
                    entity_id=voice_cfg.tts_entity,
                    service_data={
                        "media_player_entity_id": target_speaker,
                        "message": clean_msg,
                        "language": eff_lang,
                    },
                )
                return AnnouncementResult(
                    delivered=True,
                    speaker_entity=target_speaker,
                    room=room,
                    message=clean_msg,
                    tts_service="tts.speak",
                )
            except HomelabAgentError as exc:
                logger.warning(
                    "announcer | tts.speak failed (%s); falling back to tts.google_translate_say",
                    exc.message,
                )
                fallback_domain, fallback_service = "tts", "google_translate_say"
            except Exception as exc:
                logger.warning("announcer | tts.speak error: %s", exc)
                return AnnouncementResult(
                    delivered=False,
                    speaker_entity=target_speaker,
                    room=room,
                    message=clean_msg,
                    tts_service="tts.speak",
                    reason=str(exc),
                )
        else:
            fallback_domain, fallback_service = domain, service

        full_fallback = f"{fallback_domain}.{fallback_service}"
        try:
            await self._ha.call_service(
                domain=fallback_domain,
                service=fallback_service,
                entity_id=target_speaker,
                service_data={"message": clean_msg, "language": eff_lang},
            )
            return AnnouncementResult(
                delivered=True,
                speaker_entity=target_speaker,
                room=room,
                message=clean_msg,
                tts_service=full_fallback,
            )
        except Exception as exc:
            logger.warning("announcer | %s delivery failed: %s", full_fallback, exc)
            return AnnouncementResult(
                delivered=False,
                speaker_entity=target_speaker,
                room=room,
                message=clean_msg,
                tts_service=full_fallback,
                reason=str(exc),
            )

    async def on_schedule_event(self, event: BaseModel) -> None:
        """Automatically announce completed, failed, or skipped scheduled actions."""
        if not self._config.voice.announce_scheduled_results:
            return
        if not isinstance(event, ScheduleResultEvent):
            return
        if event.status == "cancelled":
            return

        entity_label = event.entity_id
        inferred_room: str | None = None
        if self._resolver is not None:
            res = await self._resolver.resolve(event.entity_id, limit=1)
            for cand in res.candidates:
                if cand.entity_id.lower() == event.entity_id.lower():
                    entity_label = cand.friendly_name or event.entity_id
                    inferred_room = cand.area_name or cand.area_id
                    break

        if event.status == "done":
            msg = f"Zamanlanmış görev tamamlandı: {entity_label}."
        elif event.status == "failed":
            detail = f" {event.detail}" if event.detail else ""
            msg = f"Zamanlanmış görev başarısız oldu: {entity_label}.{detail}".strip()
        else:
            msg = "Geciken zamanlanmış görev güvenlik kuralı nedeniyle atlandı."

        await self.announce(msg, room=inferred_room)


_PREGROUND_ATTR_KEYS: tuple[str, ...] = (
    "temperature",
    "current_temperature",
    "hvac_action",
    "brightness",
    "unit_of_measurement",
)


class PreparedPromptContext(BaseModel):
    """Unified preprocessed prompt with wake-word stripping, room context, and pre-grounding."""

    raw_text: str
    cleaned_prompt: str
    wake_word_detected: bool
    room: str | None = None
    response_style: Literal["markdown", "voice"] = "markdown"
    pre_grounded_entities: list[str] = Field(default_factory=list)
    augmented_prompt: str


def _should_fetch_live_state(
    entity_resolver: EntityResolver,
    state_reader: BaseStateVerifier | None,
    fetch_live_state: bool | None,
) -> bool:
    if state_reader is None:
        return False
    if fetch_live_state is not None:
        return fetch_live_state
    if entity_resolver.last_sync_utc is not None:
        return True
    from src.tools.ha_ws_adapter import HomeAssistantWSAdapter

    return getattr(type(state_reader), "get_state", None) is not getattr(
        HomeAssistantWSAdapter, "get_state", None
    )


async def _resolve_preground_candidates(
    entity_resolver: EntityResolver,
    cleaned_prompt: str,
    clean_room: str | None,
) -> list[EntityCandidate]:
    norm_prompt = normalize_tr(cleaned_prompt)
    if not norm_prompt:
        return []

    norm_room = normalize_tr(clean_room) if clean_room else ""
    res: EntityResolutionResult
    if clean_room and norm_room and norm_room not in norm_prompt:
        res_room = await entity_resolver.resolve(
            f"{clean_room} {cleaned_prompt}",
            limit=3,
            learn_pending=False,
        )
        if res_room.resolved:
            res = res_room
        else:
            res_raw = await entity_resolver.resolve(
                cleaned_prompt,
                limit=3,
                learn_pending=False,
            )
            if res_raw.resolved or (
                res_raw.candidates
                and (
                    not res_room.candidates
                    or res_raw.candidates[0].confidence > res_room.candidates[0].confidence
                )
            ):
                res = res_raw
            else:
                res = res_room
    else:
        res = await entity_resolver.resolve(
            cleaned_prompt,
            limit=3,
            learn_pending=False,
        )

    if res.resolved and res.candidates:
        return list(res.candidates[:1])
    return [cand for cand in res.candidates[:2] if cand.confidence >= 0.78]


async def _fetch_live_state_safe(
    state_reader: BaseStateVerifier,
    entity_id: str,
) -> EntityState | None:
    try:
        return await asyncio.wait_for(state_reader.get_state(entity_id), timeout=1.0)
    except Exception as exc:
        logger.debug("pre_ground | live state fetch skipped for '%s': %s", entity_id, exc)
        return None


async def prepare_prompt_context(
    text: str,
    config: AppConfig,
    entity_resolver: EntityResolver | None = None,
    ha_adapter: BaseStateVerifier | None = None,
    room: str | None = None,
    response_style: Literal["markdown", "voice"] = "markdown",
    fetch_live_state: bool | None = None,
) -> PreparedPromptContext:
    """Apply shared wake-word stripping, room framing, and live entity pre-grounding."""
    cleaned_prompt, wake_detected = strip_wake_prefix(text, config.voice.wake_words)
    clean_room = room.strip() if room and room.strip() else None

    pre_grounded_entities: list[str] = []
    preground_lines: list[str] = []

    if entity_resolver is not None and cleaned_prompt:
        candidates = await _resolve_preground_candidates(
            entity_resolver=entity_resolver,
            cleaned_prompt=cleaned_prompt,
            clean_room=clean_room,
        )
        if candidates:
            live_states: list[EntityState | None] = [None] * len(candidates)
            if ha_adapter is not None and _should_fetch_live_state(
                entity_resolver=entity_resolver,
                state_reader=ha_adapter,
                fetch_live_state=fetch_live_state,
            ):
                fetched = await asyncio.gather(
                    *(_fetch_live_state_safe(ha_adapter, cand.entity_id) for cand in candidates)
                )
                live_states = list(fetched)

            for idx, cand in enumerate(candidates):
                st = live_states[idx]
                live_state = cand.state
                live_attrs: dict[str, Any] = {}
                if st is not None:
                    live_state = st.state
                    live_attrs = st.attributes
                    entity_resolver.update_cached_state(cand.entity_id, st.state)

                pre_grounded_entities.append(cand.entity_id)
                oda_label = cand.area_name or cand.area_id or "-"
                state_label = live_state if live_state is not None else "bilinmiyor"
                exact_label = "evet" if idx == 0 and cand.confidence >= 0.85 else "aday"
                attr_parts = [
                    f"{k}={live_attrs[k]}"
                    for k in _PREGROUND_ATTR_KEYS
                    if k in live_attrs and live_attrs[k] is not None
                ]
                attr_suffix = f", özellikler=({', '.join(attr_parts)})" if attr_parts else ""
                preground_lines.append(
                    f"- {cand.entity_id} (ad='{cand.friendly_name}', oda='{oda_label}', "
                    f"canlı_durum='{state_label}', kesin_eşleşme={exact_label}, "
                    f"güven={cand.confidence:.2f}{attr_suffix})"
                )

    sections: list[str] = []
    if response_style == "voice":
        room_note = f" Kullanıcının bulunduğu oda: {clean_room}." if clean_room else ""
        sections.append(
            "[SESLİ KOMUT MODU] Yanıtını sesli okunmaya uygun, Markdown biçimlendirmesi olmadan, "
            f"1-2 kısa ve doğal Türkçe cümleyle ver.{room_note}"
        )
    elif clean_room:
        sections.append(f"[ODA BAĞLAMI] Kullanıcının bulunduğu oda: {clean_room}.")

    if preground_lines:
        sections.append(
            "[ÖN-ÇÖZÜMLENMİŞ VARLIKLAR (Canlı Home Assistant Verisi)]\n"
            + "\n".join(preground_lines)
        )

    if sections:
        augmented_prompt = "\n\n".join(sections) + f"\n\nKullanıcı komutu: {cleaned_prompt}"
    else:
        augmented_prompt = cleaned_prompt

    return PreparedPromptContext(
        raw_text=text,
        cleaned_prompt=cleaned_prompt,
        wake_word_detected=wake_detected,
        room=clean_room,
        response_style=response_style,
        pre_grounded_entities=pre_grounded_entities,
        augmented_prompt=augmented_prompt,
    )


