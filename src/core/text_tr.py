"""Turkish smart-home text normalization, stemming, and fuzzy matching helpers.

Zero external dependencies so tokenization and matching remain sub-millisecond
while handling Turkish diacritics, I/ı casefolding, agglutinative suffixes, and
final consonant softening (e.g. ``mutfağındaki ışıkları`` -> ``mutfak``, ``isik``).
"""

from __future__ import annotations

import math
import re
from difflib import SequenceMatcher

_TURKISH_CASE_TABLE = str.maketrans(
    {
        "İ": "i",
        "I": "ı",
    }
)

_DIACRITIC_FOLD_TABLE = str.maketrans(
    {
        "ı": "i",
        "ğ": "g",
        "ü": "u",
        "ş": "s",
        "ö": "o",
        "ç": "c",
        "â": "a",
        "î": "i",
        "û": "u",
    }
)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_NON_ENTITY_RE = re.compile(r"[^a-z0-9_.]+")
_WHITESPACE_RE = re.compile(r"\s+")

_STOP_WORDS: frozenset[str] = frozenset(
    {
        "bir",
        "ve",
        "ile",
        "olan",
        "bu",
        "su",
        "o",
        "tum",
        "butun",
        "hepsi",
        "hepsini",
        "hepsinin",
        "lutfen",
        "bana",
        "bize",
        "biraz",
        "ac",
        "acar",
        "acik",
        "acikmi",
        "acma",
        "kapa",
        "kapat",
        "kapali",
        "kapalimi",
        "yak",
        "sondur",
        "calistir",
        "ayarla",
        "durum",
        "durumu",
        "durumunu",
        "nedir",
        "ne",
        "kac",
        "derece",
        "dereceye",
        "mod",
        "modu",
        "moduna",
        "entity",
        "entitysi",
        "sonra",
        "boyunca",
        "kere",
        "kez",
        "defa",
        "the",
        "turn",
        "on",
        "off",
        "toggle",
        "set",
        "an",
        "anda",
        "suan",
        "simdi",
        "hemen",
        "acaba",
        "mi",
        "mu",
        "misin",
        "musun",
        "miyiz",
        "nasil",
        "var",
        "yok",
        "kontrol",
        "et",
        "bak",
        "soyle",
        "ver",
        "yap",
    }
)

_TWO_LETTER_ROOTS: frozenset[str] = frozenset({"ev", "su", "tv"})

# Canonical smart-home stems that must not lose a final vowel or final root 'n'.
_VOWEL_PROTECTED_STEMS: frozenset[str] = frozenset(
    {
        "klima",
        "lamba",
        "avize",
        "aydinlatma",
        "isitma",
        "isitici",
        "sogutma",
        "kombi",
        "vana",
        "perde",
        "pencere",
        "kapi",
        "oda",
        "masa",
        "banyo",
        "tuvalet",
        "antre",
        "bahce",
        "oturma",
        "calisma",
        "cocuk",
        "misafir",
        "mutfak",
        "salon",
        "koridor",
        "yatak",
        "balkon",
        "kayra",
        "supurge",
        "cati",
        "kamera",
        "su",
        "boru",
        "kutu",
        "tente",
        "jaluzi",
        "garaj",
        "teras",
        "bodrum",
        "ofis",
        "hol",
        "giris",
        "kiler",
        "merdiven",
        "ebeveyn",
        "somine",
        "termostat",
        "guneslik",
        "sicaklik",
        "petek",
        "priz",
        "isik",
        "kilit",
        "zil",
        "siren",
        "musluk",
        "batarya",
        "pil",
        "radyator",
        "vantilator",
        "aspirator",
        "televizyon",
        "hoparlor",
        "anahtar",
        "salter",
        "ampul",
        "panjur",
        "stor",
        "kepenk",
        "firin",
        "ekran",
        "tavan",
        "kazan",
        "kat",
        "ev",
        "hareket",
        "nem",
        "sensor",
        "alarm",
        "boyler",
        "sofben",
        "sarj",
        "robot",
        "dolap",
        "ocak",
        "saat",
        "akilli",
        "enerji",
        "bati",
        "dogu",
        "climate",
        "cover",
        "media",
        "apple",
        "google",
        "home",
        "echo",
        "tv",
    }
)

# Suffixes starting with Turkish buffer consonant 'n' (kaynastirma n'si).
# Only valid when attaching to a vowel-final stem and never allowed to eat a root's own 'n'.
_BUFFER_N_SUFFIXES: frozenset[str] = frozenset(
    {
        "ndakini",
        "ndekini",
        "ndakinin",
        "ndekinin",
        "ndakine",
        "ndekine",
        "ndaki",
        "ndeki",
        "ndan",
        "nden",
        "nda",
        "nde",
        "nin",
        "nun",
        "ni",
        "nu",
        "na",
        "ne",
    }
)

# Suffixes starting with Turkish buffer consonant 's' (possessive -sI/-sIn- after vowels).
# Must attach to a vowel-final stem and never truncate roots ending in 's' (e.g. teras, ofis).
_BUFFER_S_SUFFIXES: frozenset[str] = frozenset(
    {
        "sindaki",
        "sindeki",
        "sundaki",
        "sindan",
        "sinden",
        "sundan",
        "sinda",
        "sinde",
        "sunda",
        "sinin",
        "sunun",
        "sini",
        "sunu",
        "sinu",
        "suna",
        "sine",
        "sina",
        "sin",
        "sun",
        "si",
        "su",
    }
)

# Compound relational, locative, plural-possessive suffixes (longest first).
_COMPOUND_SUFFIXES: tuple[str, ...] = (
    "larindaki",
    "lerindeki",
    "larindan",
    "lerinden",
    "larinin",
    "lerinin",
    "larinda",
    "lerinde",
    "larina",
    "lerine",
    "larini",
    "lerini",
    "sindaki",
    "sindeki",
    "sundaki",
    "sindan",
    "sinden",
    "sundan",
    "sinda",
    "sinde",
    "sunda",
    "sinin",
    "sunun",
    "ndakinin",
    "ndekinin",
    "ndakini",
    "ndekini",
    "ndakine",
    "ndekine",
    "ndaki",
    "ndeki",
    "dakinin",
    "dekinin",
    "takinin",
    "tekinin",
    "dakini",
    "dekini",
    "takini",
    "tekini",
    "dakine",
    "dekine",
    "takine",
    "tekine",
    "lardan",
    "lerden",
    "larda",
    "lerde",
    "larin",
    "lerin",
    "daki",
    "deki",
    "taki",
    "teki",
    "lari",
    "leri",
    "lar",
    "ler",
    "sini",
    "sunu",
    "sinu",
    "suna",
    "sine",
    "sina",
    "ndan",
    "nden",
    "ini",
    "uni",
    "ina",
    "una",
    "ine",
    "une",
)

# Shorter inflectional / case suffixes applied iteratively (up to 2 passes).
_SHORT_SUFFIXES: tuple[str, ...] = (
    "dan",
    "den",
    "tan",
    "ten",
    "nda",
    "nde",
    "nin",
    "nun",
    "sin",
    "sun",
    "lar",
    "ler",
    "da",
    "de",
    "ta",
    "te",
    "ya",
    "ye",
    "yi",
    "yu",
    "si",
    "su",
    "sa",
    "se",
    "in",
    "un",
    "ni",
    "nu",
    "na",
    "ne",
    "ki",
)

_SINGLE_VOWEL_SUFFIXES: frozenset[str] = frozenset({"i", "u", "a", "e"})

_HARDENING_MAP: dict[str, str] = {
    "isig": "isik",
    "mutfag": "mutfak",
    "yatag": "yatak",
    "ocag": "ocak",
    "dolab": "dolap",
    "reng": "renk",
    "kapag": "kapak",
    "kilid": "kilit",
    "kanad": "kanat",
    "gecid": "gecit",
    "devr": "devir",
    "vakt": "vakit",
    "ism": "isim",
    "omr": "omur",
    "sehr": "sehir",
    "suy": "su",
}

_DOMAIN_HINT_MAP: dict[str, tuple[str, ...]] = {
    "isik": ("light", "switch"),
    "lamba": ("light", "switch"),
    "ampul": ("light", "switch"),
    "aydinlatma": ("light", "switch"),
    "spot": ("light", "switch"),
    "led": ("light", "switch"),
    "avize": ("light", "switch"),
    "light": ("light", "switch"),
    "klima": ("climate",),
    "termostat": ("climate",),
    "kombi": ("climate",),
    "petek": ("climate",),
    "radyator": ("climate",),
    "vana": ("climate",),
    "isitma": ("climate",),
    "isitici": ("climate", "switch"),
    "sogutma": ("climate",),
    "climate": ("climate",),
    "hvac": ("climate",),
    "priz": ("switch",),
    "anahtar": ("switch",),
    "salter": ("switch",),
    "switch": ("switch",),
    "plug": ("switch",),
    "perde": ("cover",),
    "panjur": ("cover",),
    "stor": ("cover",),
    "kepenk": ("cover",),
    "guneslik": ("cover",),
    "jaluzi": ("cover",),
    "tente": ("cover",),
    "cover": ("cover",),
    "tv": ("media_player",),
    "televizyon": ("media_player",),
    "hoparlor": ("media_player",),
    "homepod": ("media_player",),
    "medya": ("media_player",),
    "muzik": ("media_player",),
    "ses": ("media_player",),
    "speaker": ("media_player",),
    "vantilator": ("fan", "switch"),
    "fan": ("fan", "climate"),
    "aspirator": ("fan", "switch"),
    "supurge": ("vacuum",),
    "robot": ("vacuum",),
    "kilit": ("lock", "switch"),
    "zil": ("siren", "switch", "binary_sensor"),
    "siren": ("siren", "switch", "binary_sensor"),
    "alarm": ("siren", "switch", "binary_sensor"),
    "musluk": ("water_heater", "sensor", "switch"),
    "boyler": ("water_heater", "sensor", "switch"),
    "sofben": ("water_heater", "sensor", "switch"),
    "batarya": ("sensor",),
    "pil": ("sensor",),
    "sarj": ("sensor",),
    "kamera": ("camera",),
    "sicaklik": ("sensor", "climate"),
    "nem": ("sensor",),
    "pencere": ("binary_sensor", "cover"),
    "kapi": ("binary_sensor", "lock"),
    "sensor": ("sensor", "binary_sensor"),
    "hareket": ("binary_sensor",),
}


def normalize_tr(text: str, preserve_entity_chars: bool = False) -> str:
    """Normalize Turkish text to lowercase ASCII tokens separated by single spaces."""
    if not text:
        return ""
    folded = text.replace("I\u0307", "i").translate(_TURKISH_CASE_TABLE).lower()
    folded = folded.translate(_DIACRITIC_FOLD_TABLE)
    pattern = _NON_ENTITY_RE if preserve_entity_chars else _NON_ALNUM_RE
    cleaned = pattern.sub(" ", folded)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def _unsoften_consonant(stem: str) -> str:
    if stem in _HARDENING_MAP:
        return _HARDENING_MAP[stem]
    if len(stem) >= 4 and stem.endswith("g") and stem[-2] in "aeiou":
        return f"{stem[:-1]}k"
    return stem


_BUFFER_SUFFIX_GUARDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("n", _BUFFER_N_SUFFIXES),
    ("s", _BUFFER_S_SUFFIXES),
)


def _can_strip_suffix(work: str, suffix: str) -> bool:
    if not work.endswith(suffix):
        return False
    cand = work[: -len(suffix)]
    if len(cand) < 3 and cand not in _TWO_LETTER_ROOTS:
        return False
    for buf_char, guarded_suffixes in _BUFFER_SUFFIX_GUARDS:
        if suffix in guarded_suffixes:
            # Buffer consonants ('n', 's') only attach after a vowel and must not truncate consonant stems.
            if cand[-1] not in "aeiou" or f"{cand}{buf_char}" in _VOWEL_PROTECTED_STEMS:
                return False
    return True


def _match_known_root_with_single_vowel(work: str) -> str | None:
    if len(work) < 3 or work[-1] not in _SINGLE_VOWEL_SUFFIXES:
        return None
    base = work[:-1]
    unsoftened = _unsoften_consonant(base)
    if unsoftened in _VOWEL_PROTECTED_STEMS or base in _HARDENING_MAP:
        return unsoftened
    return None


def _strip_first_person_possessive(work: str) -> str:
    if work in _VOWEL_PROTECTED_STEMS or len(work) < 3:
        return work
    if work.endswith("m") and work[:-1] in _VOWEL_PROTECTED_STEMS:
        return work[:-1]
    if work.endswith(("im", "um")) and len(work) >= 4:
        base = work[:-2]
        unsoftened = _unsoften_consonant(base)
        if unsoftened in _VOWEL_PROTECTED_STEMS or base in _HARDENING_MAP:
            return unsoftened
    return work


def stem_tr_token(token: str) -> str:
    """Light, deterministic Turkish suffix stripper preserving short roots."""
    work = normalize_tr(token)
    if " " in work:
        work = work.split(" ", 1)[0]
    if len(work) <= 2 or work in _VOWEL_PROTECTED_STEMS:
        return _HARDENING_MAP.get(work, work)
    if direct_base := _match_known_root_with_single_vowel(work):
        return direct_base

    stripped = False
    for suffix in _COMPOUND_SUFFIXES:
        if _can_strip_suffix(work, suffix):
            work = work[: -len(suffix)]
            stripped = True
            break

    for _ in range(2):
        unsoftened = _unsoften_consonant(work)
        if len(work) <= 2 or unsoftened in _VOWEL_PROTECTED_STEMS:
            return unsoftened
        if direct_base := _match_known_root_with_single_vowel(work):
            return direct_base
        matched = False
        for suffix in _SHORT_SUFFIXES:
            if _can_strip_suffix(work, suffix):
                work = work[: -len(suffix)]
                matched = True
                stripped = True
                break
        if not matched:
            break

    if (
        not stripped
        and len(work) >= 4
        and work not in _VOWEL_PROTECTED_STEMS
        and work[-1] in _SINGLE_VOWEL_SUFFIXES
    ):
        work = work[:-1]
    elif (
        stripped
        and work not in _VOWEL_PROTECTED_STEMS
        and work[-1] in ("i", "u")
        and len(work) >= 3
        and (
            len(work) >= 4 and work[-2] not in "aeiou"
            or work[:-1] in _TWO_LETTER_ROOTS
            or _unsoften_consonant(work[:-1]) in _VOWEL_PROTECTED_STEMS
            or work[:-1] in _HARDENING_MAP
        )
    ):
        work = work[:-1]

    work = _strip_first_person_possessive(work)
    return _unsoften_consonant(work)


def stem_tr_phrase(text: str, strip_stopwords: bool = True) -> tuple[str, tuple[str, ...]]:
    """Return `(joined_stems, tuple_of_stems)` for normalized Turkish smart-home phrases."""
    norm = normalize_tr(text)
    if not norm:
        return "", ()
    stems: list[str] = []
    seen: set[str] = set()
    for raw_tok in norm.split():
        if strip_stopwords and raw_tok in _STOP_WORDS:
            continue
        stem = stem_tr_token(raw_tok)
        if strip_stopwords and stem in _STOP_WORDS:
            continue
        if stem and stem not in seen:
            seen.add(stem)
            stems.append(stem)
    if not stems and strip_stopwords:
        # Fallback if all tokens were command verbs/stopwords (e.g. "ac")
        for raw_tok in norm.split():
            stem = stem_tr_token(raw_tok)
            if stem and stem not in seen:
                seen.add(stem)
                stems.append(stem)
    return " ".join(stems), tuple(stems)


def infer_domain_hints(text: str) -> tuple[str, ...]:
    """Infer likely Home Assistant domain names from Turkish or English keywords."""
    norm = normalize_tr(text)
    if not norm:
        return ()
    domains: list[str] = []
    seen: set[str] = set()
    for raw_tok in norm.split():
        for key in (raw_tok, stem_tr_token(raw_tok)):
            for domain in _DOMAIN_HINT_MAP.get(key, ()):
                if domain not in seen:
                    seen.add(domain)
                    domains.append(domain)
    return tuple(domains)


def _char_trigram_cosine(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    pa = f"<{a}>"
    pb = f"<{b}>"
    grams_a: dict[str, int] = {}
    grams_b: dict[str, int] = {}
    for i in range(max(1, len(pa) - 2)):
        g = pa[i : i + 3]
        grams_a[g] = grams_a.get(g, 0) + 1
    for i in range(max(1, len(pb) - 2)):
        g = pb[i : i + 3]
        grams_b[g] = grams_b.get(g, 0) + 1

    dot = sum(val * grams_b.get(g, 0) for g, val in grams_a.items())
    norm_a = math.sqrt(sum(v * v for v in grams_a.values()))
    norm_b = math.sqrt(sum(v * v for v in grams_b.values()))
    if norm_a <= 1e-12 or norm_b <= 1e-12:
        return 0.0
    return float(dot / (norm_a * norm_b))


def fuzzy_phrase_similarity(
    query_norm: str,
    query_stems: tuple[str, ...],
    candidate_norm: str,
    candidate_stems: tuple[str, ...],
) -> float:
    """Compute a bounded `[0.0, 1.0]` similarity blending stem overlap, 3-grams, and edit ratio."""
    if not query_norm or not candidate_norm:
        return 0.0
    if query_norm == candidate_norm:
        return 1.0
    if query_stems and candidate_stems and query_stems == candidate_stems:
        return 0.99

    q_set = set(query_stems)
    c_set = set(candidate_stems)
    if q_set and c_set:
        intersection = len(q_set & c_set)
        jaccard = intersection / len(q_set | c_set)
        q_coverage = intersection / len(q_set)
        stem_score = 0.65 * q_coverage + 0.35 * jaccard
    else:
        stem_score = 0.0

    q_joined = " ".join(query_stems) or query_norm
    c_joined = " ".join(candidate_stems) or candidate_norm
    trigram_score = _char_trigram_cosine(q_joined, c_joined)
    seq_score = SequenceMatcher(None, q_joined, c_joined).ratio()

    combined = 0.50 * stem_score + 0.30 * trigram_score + 0.20 * seq_score
    return min(1.0, max(0.0, combined))
