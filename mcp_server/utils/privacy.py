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

_PII_PATTERNS: list[tuple[str, Pattern[str]]] = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("PHONE", re.compile(r"\+?\d[\d\s()-]{8,}\d")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]

_nlp = None


def _get_nlp() -> Any:
    """Lazy process-wide spaCy NER (en_core_web_sm; ru-проза не парсится — ок)."""
    global _nlp
    if _nlp is None:
        import spacy

        _nlp = spacy.load("en_core_web_sm")
    return _nlp


def _ru_personas() -> frozenset[str]:
    """Словарь персон проекта: config rag.ru_personas + классы rag.synonyms.

    Каждая персона расширяется синонимами в ОБЕ стороны (тот же _canon-класс,
    что в graph_miners: «Лили»/«Lily»/«лисёныш» → одна персона). Config —
    единственный источник имён; в коде ничего не хардкодится.
    """
    from config import config
    from rag.synonyms import load_synonyms

    personas: set[str] = set(config.get("rag", "ru_personas", default=None) or [])
    syn = load_synonyms()
    for name in list(personas):
        low = name.lower()
        # обе стороны: собственные синонимы + все ключи, чьим синонимом является имя
        personas |= set(syn.get(low, []))
        personas |= {k for k, vs in syn.items() if low in (v.lower() for v in vs)}
    return frozenset(personas)


_ru_re_cache: tuple[frozenset[str], Pattern[str]] | None = None


def _ru_persona_re(personas: frozenset[str]) -> Pattern[str]:
    r"""Word-boundary regex по словарю (re.UNICODE — \b работает и на кириллице)."""
    global _ru_re_cache
    if _ru_re_cache is None or _ru_re_cache[0] != personas:
        alts = "|".join(sorted((re.escape(p) for p in personas), key=len, reverse=True))
        _ru_re_cache = (personas, re.compile(rf"\b(?:{alts})\b", re.IGNORECASE))
    return _ru_re_cache[1]


def sanitize(text: str, *, use_ner: bool = True) -> tuple[str, dict[str, str]]:
    """Replace secrets/PII with stable typed placeholders. Reverse map не персистится."""
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

    # ru-persona tier: структурный словарь ДЕШЕВЛЕ spaCy и ловит то, что
    # en-NER пропускает (sentence-initial кириллические имена) — поэтому ДО NER.
    # Работает и при use_ner=False (словарь не зависит от NER-доступности).
    try:
        personas = _ru_personas()
        if personas:

            def _sub_ru(match: re.Match[str]) -> str:
                return _placeholder("PERSON_RU", match.group(0))

            out = _ru_persona_re(personas).sub(_sub_ru, out)
    except Exception:  # noqa: S110 — словарь недоступен: следующие тиры отработают
        pass

    if use_ner:
        try:
            doc = _get_nlp()(out)
            # span'ы уже вставленных ⟨...⟩ placeholder'ов — NER их не перезатирает
            taken = [(m.start(), m.end()) for m in re.finditer("⟨[^⟩]*⟩", out)]
            # reversed: спаны справа не смещают офсеты слева
            for ent in reversed(doc.ents):
                # Garbage guard: на ru/lorem-тексте en-модель выдаёт мусорные
                # спаны (целая фраза, "D"*200) — маскируем только короткие.
                # Кириллический спан >1 токена — проза, за которую en-модель
                # берётся после вставки placeholder'ов — тоже мусор.
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
        except Exception:  # noqa: S110 — NER недоступен: regex-тир уже отработал
            pass
    return out, mapping
