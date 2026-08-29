"""Multi-stage graph consolidation (B1.3): raw episodes → entities + links.

Stage 1 MINE    — L3 episodes within the window.
Stage 2 EXTRACT — deterministic RU+EN relation patterns between named entities.
Stage 3 LINK    — find_or_add_entity both sides + add_edge (edge dedup is free:
                  epi_edges PK is (source_id, target_id, relation)).

# ponytail: regex extraction covers explicit patterns only, and RU inflected
# forms ("Бориса" vs "Борис") count as separate entities — upgrade path is an
# LLM extractor wired behind this same interface if link density proves too low.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from shared.constants import DB_NAME

# Binary patterns: "<Subject> <relation phrase> <Object>" — both sides become
# entities. Order matters only for readability; all are applied.
_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(
            r"(?P<sub>[A-ZА-ЯЁ][\wа-яё-]{1,29})\s+(?:works\s+(?:at|for)|работает\s+(?:в|на))\s+(?P<obj>[A-ZА-ЯЁ][\wа-яё-]{1,29})", re.IGNORECASE
        ),
        "works_with",
        "organization",
    ),
    (re.compile(r"(?P<sub>[A-ZА-ЯЁ][\wа-яё-]{1,29})\s+(?:knows|знает)\s+(?P<obj>[A-ZА-ЯЁ][\wа-яё-]{1,29})", re.IGNORECASE), "knows", "person"),
    (
        re.compile(r"(?P<sub>[A-ZА-ЯЁ][\wа-яё-]{1,29})\s+(?:is\s+friends\s+with|дружит\s+с)\s+(?P<obj>[A-ZА-ЯЁ][\wа-яё-]{1,29})", re.IGNORECASE),
        "friend_of",
        "person",
    ),
    (
        re.compile(r"(?P<sub>[A-ZА-ЯЁ][\wа-яё-]{1,29})\s+(?:is\s+part\s+of|входит\s+в|часть)\s+(?P<obj>[A-ZА-ЯЁ][\wа-яё-]{1,29})", re.IGNORECASE),
        "member_of",
        "organization",
    ),
    (
        re.compile(r"(?P<sub>[A-ZА-ЯЁ][\wа-яё-]{1,29})\s+(?:met|встретил\s+|встретила\s+)(?P<obj>[A-ZА-ЯЁ][\wа-яё-]{1,29})", re.IGNORECASE),
        "met",
        "person",
    ),
]

# First-person discovery: "встретил Бориса", "talked to Claire" — creates the
# person entity without an edge (no subject entity exists for "I").
_DISCOVERY = re.compile(
    r"(?:встретил|встретилась|встретил(?:а|и)\s+с|поговорил(?:а)?\s+с|позвонил(?:а)?\s+|написал(?:а)?\s+|met|talked\s+to|called)\s+(?:с\s+)?(?P<name>[A-ZА-ЯЁ][\wа-яё-]{1,29})",
    re.IGNORECASE,
)

# Capitalized tokens that are sentence starts / pronouns, not names.
_STOP_NAMES = {"I", "The", "A", "An", "This", "That", "We", "It", "В", "На", "Мы", "Он", "Она", "Они", "Это", "Сегодня"}


@dataclass
class ExtractedLink:
    subject: str
    object: str
    relation: str
    object_type: str


def _is_name(token: str) -> bool:
    return len(token) >= 2 and token not in _STOP_NAMES


def extract_links(text: str) -> list[ExtractedLink]:
    """Stage 2: pull subject-relation-object links out of free text."""
    links: list[ExtractedLink] = []
    for pattern, relation, obj_type in _PATTERNS:
        for m in pattern.finditer(text):
            sub, obj = m["sub"], m["obj"]
            if _is_name(sub) and _is_name(obj):
                links.append(ExtractedLink(sub, obj, relation, obj_type))
    return links


def discover_entities(text: str) -> list[str]:
    """First-person mentions: person names worth creating even without an edge."""
    return [m["name"] for m in _DISCOVERY.finditer(text) if _is_name(m["name"])]


async def build_from_episodes(cm: Any, user_id: str, layer: str = "user", window_hours: int = 24) -> dict[str, int]:
    """Run the 3-stage pipeline over recent L3 episodes. Returns counters."""
    from graph.epistemic import EpistemicGraph

    graph = EpistemicGraph(cm=cm, layer=layer)
    conn = await cm.get(DB_NAME)
    cutoff = time.time() - window_hours * 3600
    rows = await (
        await conn.execute(
            "SELECT summary FROM episodes WHERE layer=? AND user_id=? AND created_at > ?",
            (layer, user_id, cutoff),
        )
    ).fetchall()

    stats = {"episodes": len(rows), "entities": 0, "edges": 0}
    seen: set[tuple[int, int, str]] = set()
    for row in rows:
        summary = row["summary"] or ""
        for link in extract_links(summary):
            sub_id, c1 = await graph.find_or_add_entity(user_id, link.subject, "person")
            obj_id, c2 = await graph.find_or_add_entity(user_id, link.object, link.object_type)
            stats["entities"] += int(c1) + int(c2)
            key = (sub_id, obj_id, link.relation)
            if key in seen:
                continue
            seen.add(key)
            await graph.add_edge(sub_id, obj_id, link.relation)
            stats["edges"] += 1
        for name in discover_entities(summary):
            _, created = await graph.find_or_add_entity(user_id, name, "person")
            stats["entities"] += int(created)
    return stats
