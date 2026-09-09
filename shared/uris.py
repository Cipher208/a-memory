"""Stage 2 Plan A: ariel URI scheme — stable references across stores.

Формат: ``ariel://<layer>/<store>/<key>``

  ariel://user/fact/fact:postgres_stack   core_memory (layer + key)
  ariel://user/wiki/work_notes/refs-x.md  wiki_index (layer + file_path)
  ariel://user/graph/node/42              epi_nodes (node_id)
  ariel://user/episode/123                episodes (episode_id)
  ariel://user/l0/9001                    l0_journal (id)
  ariel://peer/<name>/…                   ЗАРЕЗЕРВИРОВАНО (peers/tunnels, не строится)

URI определяет слой + хранилище + ключ; user_id — параметр вызова resolve
(изоляция сохраняется: резолв требует user_id и чужое не найдёт). Ноль
миграций: URI выводится из существующих ключей, ничего не пере-хэшируется.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SCHEME = "ariel://"
# Valid store segments are validated inline in parse_uri ("graph", not
# "graph/node" — the tuple below was a stale doc-as-code duplicate).
RESERVED_PREFIXES = ("peer/",)


def fact_uri(layer: str, key: str) -> str:
    return f"{SCHEME}{layer}/fact/{key}"


def wiki_uri(layer: str, file_path: str) -> str:
    # путь сохраняется как есть (с .md) — wm.get резолвит по полному имени
    return f"{SCHEME}{layer}/wiki/{file_path!s}"


def node_uri(layer: str, node_id: int) -> str:
    return f"{SCHEME}{layer}/graph/node/{int(node_id)}"


def episode_uri(layer: str, episode_id: int) -> str:
    return f"{SCHEME}{layer}/episode/{int(episode_id)}"


def l0_uri(layer: str, rid: int) -> str:
    return f"{SCHEME}{layer}/l0/{int(rid)}"


def parse_uri(uri: str) -> dict[str, Any] | None:
    """``{'layer', 'store', 'key', 'rest'}`` | None (не ariel-URI / мусор).

    rest — хвост после store (для wiki это путь; для graph/node — id).
    Зарезервированные namespace (peer/) парсятся с флагом reserved=True.
    """
    if not isinstance(uri, str) or not uri.startswith(SCHEME):
        return None
    rest = uri[len(SCHEME) :].strip("/")
    if not rest:
        return None
    parts = rest.split("/", 2)
    if len(parts) < 2:
        return None
    layer, store = parts[0].strip(), parts[1].strip()
    tail = parts[2].strip() if len(parts) > 2 else ""
    if not layer or not store:
        return None
    # peer/ и прочие резервы живут на store-позиции (слоя всегда 2 сегмента)
    if store == "peer" or any(store == p.rstrip("/") for p in RESERVED_PREFIXES):
        return {"layer": layer, "store": "peer", "key": tail, "rest": rest, "reserved": True}
    if store not in ("fact", "wiki", "graph", "episode", "l0"):
        return None
    if store == "graph":
        if not tail.startswith("node/") or not tail[5:].isdigit():
            return None
        return {"layer": layer, "store": "graph", "key": tail[5:], "rest": tail, "node_id": int(tail[5:])}
    if store in ("episode", "l0"):
        if not tail.isdigit():
            return None
        return {"layer": layer, "store": store, "key": tail, "rest": tail, "id": int(tail)}
    if not tail:
        return None
    return {"layer": layer, "store": store, "key": tail, "rest": tail}


async def resolve_uri(
    cm: Any,
    layer: str,
    user_id: str,
    uri: str,
    *,
    wiki: Any | None = None,
) -> dict[str, Any] | None:
    """URI → содержимое. None = не найдено/не ariel-URI; peer → ValueError.

    user_id обязателен в вызове — изоляция: чужие записи по URI не видны.
    """
    parsed = parse_uri(uri)
    if parsed is None:
        return None
    if parsed.get("reserved"):
        raise ValueError(f"URI namespace reserved (not built): {uri!r}")
    store = parsed["store"]
    from shared.constants import DB_NAME

    conn = await cm.get(DB_NAME)
    if store == "fact":
        from core.memory import CoreMemory

        entry = await CoreMemory(cm=cm, layer=layer).get(user_id, parsed["key"])
        if entry is None:
            return None
        return {
            "store": "fact",
            "layer": layer,
            "key": entry.key,
            "value": entry.value,
            "importance": entry.importance,
            "memory_kind": entry.memory_kind,
            "entry_id": entry.entry_id,
            "uri": fact_uri(layer, entry.key),
        }
    if store == "wiki":
        if wiki is None:
            from wiki.manager import WikiManager

            wiki = WikiManager(layer=layer, cm=cm)
        wentry = await wiki.get(parsed["key"])
        if wentry is None:
            return None
        return {
            "store": "wiki",
            "layer": layer,
            "path": wentry.file_path,
            "title": wentry.title,
            "wiki_type": wentry.wiki_type,
            "content": wentry.content,
            "uri": wiki_uri(layer, wentry.file_path),
        }
    if store == "graph":
        row = await (
            await conn.execute(
                "SELECT node_id, content, node_type FROM epi_nodes WHERE node_id=? AND layer=?",
                (parsed["node_id"], layer),
            )
        ).fetchone()
        if row is None:
            return None
        return {
            "store": "graph",
            "layer": layer,
            "node_id": int(row["node_id"]),
            "content": str(row["content"]),
            "node_type": str(row["node_type"]),
            "uri": node_uri(layer, int(row["node_id"])),
        }
    if store == "episode":
        row = await (
            await conn.execute(
                "SELECT episode_id, summary FROM episodes WHERE episode_id=? AND layer=?",
                (parsed["id"], layer),
            )
        ).fetchone()
        if row is None:
            return None
        return {
            "store": "episode",
            "layer": layer,
            "episode_id": int(row["episode_id"]),
            "summary": str(row["summary"]),
            "uri": episode_uri(layer, int(row["episode_id"])),
        }
    if store == "l0":
        row = await (
            await conn.execute(
                "SELECT id, text, ts, event FROM l0_journal WHERE id=? AND layer=?",
                (parsed["id"], layer),
            )
        ).fetchone()
        if row is None:
            return None
        return {
            "store": "l0",
            "layer": layer,
            "id": int(row["id"]),
            "text": str(row["text"]),
            "ts": float(row["ts"]),
            "event": str(row["event"]),
            "uri": l0_uri(layer, int(row["id"])),
        }
    return None
