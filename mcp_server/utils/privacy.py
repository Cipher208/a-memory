import contextlib
import re
from re import Pattern
from typing import Any

_CREDENTIAL_PATTERNS: list[Pattern[str]] = [
    re.compile(r"\b(sk-[A-Za-z0-9]{20,})\b"),
    re.compile(r"\b(sk-ant-[A-Za-z0-9-]{20,})\b"),
    re.compile(r"\b(ghp_[A-Za-z0-9]{36})\b"),
    re.compile(r"\b(gho_[A-Za-z0-9]{36})\b"),
    re.compile(r"\b(ghs_[A-Za-z0-9]{36})\b"),
    re.compile(r"\b(ghr_[A-Za-z0-9]{36})\b"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{20,})\b"),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
    re.compile(r"\b(AIza[0-9A-Za-z_-]{35})\b"),
    re.compile(r"\b(sk_live_[0-9a-zA-Z]{24,})\b"),
    re.compile(r"\b(pk_live_[0-9a-zA-Z]{24,})\b"),
    re.compile(r"\b(sk_test_[0-9a-zA-Z]{24,})\b"),
    re.compile(r"\b([0-9]{10}:[A-Za-z0-9_-]{35})\b"),
    re.compile(r"\b(Bearer\s+[A-Za-z0-9_\-\.]{20,})\b", re.IGNORECASE),
    re.compile(r"<private>.*?</private>", re.DOTALL),
    re.compile(r"<secret>.*?</secret>", re.DOTALL),
    re.compile(r"<credentials>.*?</credentials>", re.DOTALL),
]


def strip_secrets(text: str, replacement: str = "[REDACTED]") -> str:
    if not text:
        return text
    result = text
    for pattern in _CREDENTIAL_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


# G0 privacy gate: secrets/PII → stable typed placeholders ⟨KIND_N⟩.
_NER_LABELS = {"PERSON", "ORG", "GPE", "LOC"}
# ru_core_news_sm (S19): PER/ORG/LOC instead of the en-model's PERSON/ORG/GPE/LOC.
_RU_NER_LABELS = {"PER", "ORG", "LOC"}

_PII_PATTERNS: list[tuple[str, Pattern[str]]] = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("PHONE", re.compile(r"\+?\d[\d\s()-]{8,}\d")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]

_nlp = None
_ru_nlp = None
_ner_breaker = None


def _get_nlp() -> Any:
    """Lazy process-wide spaCy NER (en_core_web_sm; Cyrillic prose is not parsed — fine)."""
    global _nlp
    if _nlp is None:
        import spacy

        _nlp = spacy.load("en_core_web_sm")
    return _nlp


def _ru_ner_enabled() -> bool:
    """S19: ru_core_news_sm on the privacy gate.

    Default on — on failure the breaker degrades to the en-model/regex;
    disable via config rag.ru_ner=false.
    """
    from config import config

    return bool(config.get("rag", "ru_ner", default=True))


def _get_ru_nlp() -> Any:
    """Lazy ru_core_news_sm + circuit breaker (_embedding_breaker pattern).

    3 load/run failures in a row → breaker open for 60s; sanitize then runs
    on the en-model + dictionary + regex. Model not installed → None without
    failures (degradation is a normal mode, not a breakage).
    """
    global _ru_nlp, _ner_breaker
    if _ru_nlp is not None:
        return _ru_nlp
    if _ner_breaker is None:
        from shared.circuit_breaker import breaker_registry

        _ner_breaker = breaker_registry.get("ru_ner_model", threshold=3, recovery_timeout=60.0)
    if not _ner_breaker.allow_request():
        return None
    try:
        import spacy

        _ru_nlp = spacy.load("ru_core_news_sm")
        _ner_breaker.record_success()
    except Exception:
        # Not installed or broken — breaker accumulates failures, retry only
        # after recovery_timeout (not on every message).
        _ner_breaker.record_failure()
        _ru_nlp = None
    return _ru_nlp


def _ru_personas() -> frozenset[str]:
    """Project persona dictionary: config rag.ru_personas + rag.synonyms classes.

    Each persona is expanded with synonyms in BOTH directions (same _canon
    class as in graph_miners: "Lili"/"Lily"/"lisyonysh" → one persona). The
    config is the single source of names; nothing is hardcoded in code.
    """
    from config import config
    from rag.synonyms import load_synonyms

    personas: set[str] = set(config.get("rag", "ru_personas", default=None) or [])
    syn = load_synonyms()
    for name in list(personas):
        low = name.lower()
        # both directions: own synonyms + all keys for which the name is a synonym
        personas |= set(syn.get(low, []))
        personas |= {k for k, vs in syn.items() if low in (v.lower() for v in vs)}
    return frozenset(personas)


_ru_re_cache: tuple[frozenset[str], Pattern[str]] | None = None


def _ru_persona_re(personas: frozenset[str]) -> Pattern[str]:
    r"""Word-boundary regex over the dictionary (re.UNICODE — \b works on Cyrillic too)."""
    global _ru_re_cache
    if _ru_re_cache is None or _ru_re_cache[0] != personas:
        alts = "|".join(sorted((re.escape(p) for p in personas), key=len, reverse=True))
        _ru_re_cache = (personas, re.compile(rf"\b(?:{alts})\b", re.IGNORECASE))
    return _ru_re_cache[1]


def sanitize(text: str, *, use_ner: bool = True) -> tuple[str, dict[str, str]]:
    """Replace secrets/PII with stable typed placeholders. Reverse map is not persisted."""
    if not text:
        return text, {}

    mapping: dict[str, str] = {}
    counters: dict[str, int] = {}

    def _placeholder(kind: str, value: str) -> str:
        for key, existing in mapping.items():
            if existing == value:
                return key
        counters[kind] = counters.get(kind, 0) + 1
        key = f"⟨{kind}_{counters[kind]}⟩"
        mapping[key] = value
        return key

    out = text
    for kind, pattern in _PII_PATTERNS:

        def _sub(match: re.Match[str], _kind: str = kind) -> str:
            return _placeholder(_kind, match.group(0))

        out = pattern.sub(_sub, out)

    # ru-persona tier: the structured dictionary is CHEAPER than spaCy and
    # catches what en-NER misses (sentence-initial Cyrillic names) — hence
    # BEFORE NER. Also works with use_ner=False (dictionary does not depend
    # on NER availability).
    try:
        personas = _ru_personas()
        if personas:

            def _sub_ru(match: re.Match[str]) -> str:
                return _placeholder("PERSON_RU", match.group(0))

            out = _ru_persona_re(personas).sub(_sub_ru, out)
    except Exception:  # noqa: S110 — dictionary unavailable: the next tiers will handle it
        pass

    if use_ner:
        # S19: ru-NER for Cyrillic (ru_core_news_sm), en-NER for Latin script.
        # ru-model garbage guard (S19 spec: ru-NER is noisy on short texts):
        # PER/ORG/LOC ≤ 4 tokens AND ≥ 2 chars; 1-char and all-digit spans
        # are tokenizer noise, not a person. ru-model failure → en-path as is.
        has_cyr_input = any("\u0400" <= ch <= "\u04ff" for ch in out)
        ru_doc = None
        if has_cyr_input and _ru_ner_enabled():
            with contextlib.suppress(Exception):
                ru_nlp = _get_ru_nlp()
                if ru_nlp is not None:
                    ru_doc = ru_nlp(out)
        if ru_doc is not None:
            try:
                taken = [(m.start(), m.end()) for m in re.finditer("⟨[^⟩]*⟩", out)]
                for ent in reversed(ru_doc.ents):
                    # Garbage guard (S19, spec "ru-NER is noisy"): PER without
                    # a surname is almost always a capitalized common noun
                    # ("Kisonka vyshla"); real names the ru-model gives as
                    # 2-3 tokens. Single-token ORG/LOC are legitimate
                    # (Baltschug, Moskva) — keep them.
                    is_noisy_per = ent.label_ == "PER" and len(ent) < 2
                    if (
                        ent.label_ in _RU_NER_LABELS
                        and not is_noisy_per
                        and len(ent) <= 4
                        and len(ent.text) >= 2
                        and not ent.text.replace(" ", "").isdigit()
                        and not any(s < ent.end_char and ent.start_char < e for s, e in taken)
                    ):
                        key = _placeholder(ent.label_, ent.text)
                        out = out[: ent.start_char] + key + out[ent.end_char :]
            except Exception:  # noqa: S110 — ru-NER run failed: the next tier will handle it
                pass
        # en-NER only on Latin script: purely Cyrillic text gets parsed by
        # the en-model into garbage (the guard filtered it all out, but the
        # inference was wasted) — skip.
        has_latin = any(ch.isascii() and ch.isalpha() for ch in out)
        if has_latin:
            try:
                doc = _get_nlp()(out)
                # spans of already-inserted ⟨...⟩ placeholders — NER must not overwrite them
                taken = [(m.start(), m.end()) for m in re.finditer("⟨[^⟩]*⟩", out)]
                # reversed: spans on the right do not shift offsets on the left
                for ent in reversed(doc.ents):
                    # Garbage guard: on ru/lorem text the en-model emits garbage
                    # spans (a whole phrase, "D"*200) — mask only short ones.
                    # A Cyrillic span of >1 token is prose that the en-model
                    # grabs after placeholder insertion — garbage too.
                    has_cyr = any("\u0400" <= ch <= "\u04ff" for ch in ent.text)
                    if (
                        ent.label_ in _NER_LABELS
                        and len(ent) <= 4
                        and len(ent.text) <= 40
                        and not (has_cyr and len(ent) > 1)
                        and not any(s < ent.end_char and ent.start_char < e for s, e in taken)
                    ):
                        key = _placeholder(ent.label_, ent.text)
                        out = out[: ent.start_char] + key + out[ent.end_char :]
            except Exception:  # noqa: S110 — NER unavailable: the regex tier already ran
                pass
    return out, mapping
