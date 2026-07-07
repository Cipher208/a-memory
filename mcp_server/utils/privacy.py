import re
from typing import List, Pattern


_CREDENTIAL_PATTERNS: List[Pattern] = [
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


def has_secrets(text: str) -> bool:
    if not text:
        return False
    for pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(text):
            return True
    return False


def get_redacted_preview(text: str, max_length: int = 100) -> str:
    redacted = strip_secrets(text)
    if len(redacted) > max_length:
        return redacted[:max_length] + "..."
    return redacted
