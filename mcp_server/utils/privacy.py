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

    if use_ner:
        try:
            doc = _get_nlp()(out)
            # reversed: спаны справа не смещают офсеты слева
            for ent in reversed(doc.ents):
                # Garbage guard: на ru/lorem-тексте en-модель выдаёт мусорные
                # спаны (целая фраза, "D"*200) — маскируем только короткие.
                if ent.label_ in _NER_LABELS and len(ent) <= 4 and len(ent.text) <= 40:
                    key = _placeholder(ent.label_, ent.text)
                    out = out[: ent.start_char] + key + out[ent.end_char :]
        except Exception:  # noqa: S110 — NER недоступен: regex-тир уже отработал
            pass
    return out, mapping
