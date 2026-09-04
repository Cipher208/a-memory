"""F-T7 session-close extraction: A5-regex patterns → staged proposals.

На закрытии сессии тексты сессии (ctx['session_texts'] или L1 ring)
сканируются на preference / experience / lesson(anti-pattern) фразы;
каждое совпадение → features.staging.propose (kind='core_write',
source='session_close') — ревью-тир применяет, прямых L4-записей нет.

E6: error_pattern — это typed schema, НЕ MemoryKind (в shared.memory_types
13 kinds, error_pattern среди них нет). Lesson-записи идут kind='fact'
префиксом ключа lesson: + теги lesson/error_pattern в payload —
консолидация/минеры поднимут typed schema из тегов.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_TRIGGERS: dict[str, tuple[str, ...]] = {
    "preference": ("предпочитаю", "люблю", "не люблю", "я за"),
    "experience": ("оказалось что", "работает", "не сработало"),
    "lesson": ("больше не делать", "не повторять", "запомни ошибка", "провалилось"),
}
_IMPORTANCE: dict[str, float] = {"preference": 0.6, "experience": 0.5, "lesson": 0.7}

_SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text) if len(s.strip()) >= 8]


def _slug(sentence: str, ptype: str) -> str:
    words = [w for w in re.findall(r"[а-яёa-z0-9]+", sentence.lower()) if len(w) > 2][:4]
    return f"{ptype}:" + "_".join(words) if words else f"{ptype}:misc"


def match_patterns(text: str) -> list[tuple[str, str]]:
    """[(ptype, sentence)] — максимум одно совпадение на предложение."""
    out: list[tuple[str, str]] = []
    for sentence in _sentences(text):
        low = sentence.lower()
        for ptype, triggers in _TRIGGERS.items():
            if any(t in low for t in triggers):
                out.append((ptype, sentence))
                break
    return out


async def extract_and_stage(mem: Any, user_id: str, session_texts: list[str]) -> dict[str, Any]:
    """Паттерны A5 → staging propose (source='session_close'). Best-effort.

    mem не используется напрямую (propose пишет через connection_manager) —
    параметр оставлен для сигнатуры вызова из хуков.
    """
    from features.staging import propose

    patterns: dict[str, int] = {"preference": 0, "experience": 0, "lesson": 0}
    for text in session_texts:
        for ptype, sentence in match_patterns(text):
            tags = ["session_close", ptype]
            if ptype == "lesson":
                tags += ["lesson", "error_pattern"]
            payload: dict[str, Any] = {
                "key": _slug(sentence, ptype),
                "value": sentence[:500],
                "importance": _IMPORTANCE[ptype],
                "tags": tags,
            }
            try:
                await propose("session_close", "core_write", user_id, "user", payload)
                patterns[ptype] += 1
            except Exception as exc:  # S112: best-effort — сбой стейджинга не роняет сессию
                logger.debug("session_close propose failed: %s", exc)
                continue
    return {"staged": sum(patterns.values()), "patterns": patterns}
