"""Phase H Task 3: import_chat — 4 export formats → L0 via gates (origin=import).

Each parser normalizes its export shape to (role, text, ts); import_records
captures every record into l0_journal with event='import', raw_type='import'
(import lines are not deterministic — classify_raw would misread them) and
runs distill_and_route directly (score=0.6, importance decided by the kind
gates). Rows are marked with the g1 config-hash decision so the watermark
replay never re-processes them.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from shared.connection import connection_manager


@pytest.fixture
async def import_db(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)  # патчим base_dir, не подменяем объект
    connection_manager._conns.clear()  # cached conns pin the old tmp dir
    from shared.migrations import migration_manager

    await migration_manager.migrate()
    yield tmp_path
    connection_manager._conns.clear()


async def _l0_rows() -> list[Any]:
    conn = await connection_manager.get("memory.db")
    return list(await (await conn.execute("SELECT id, event, raw_type, status, text, decisions FROM l0_journal ORDER BY id")).fetchall())


def _claude_file(tmp_path: Any) -> str:
    p = tmp_path / "claude-conversations.json"
    p.write_text(
        json.dumps(
            [
                {
                    "uuid": "c1",
                    "name": "conv",
                    "messages": [
                        {"role": "user", "content": "я решила перейти на PostgreSQL для проекта"},
                        {"role": "assistant", "content": [{"type": "text", "text": "хорошо, миграция на postgres займёт время"}]},
                    ],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(p)


def _chatgpt_file(tmp_path: Any) -> str:
    p = tmp_path / "chatgpt.json"
    p.write_text(
        json.dumps(
            [
                {
                    "title": "t",
                    "mapping": {
                        "n0": {"message": {"author": {"role": "system"}, "content": {"parts": ["system prompt text"]}}},
                        "n1": {"message": {"author": {"role": "user"}, "content": {"parts": ["я решила перейти на PostgreSQL для проекта"]}}},
                        "n2": {"message": {"author": {"role": "assistant"}, "content": {"parts": ["хорошо, миграция на postgres займёт время"]}}},
                        "n3": {},  # node without message — must be skipped
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(p)


def _memory_json_file(tmp_path: Any) -> str:
    p = tmp_path / "memory.json"
    p.write_text(
        json.dumps(
            [{"key": "db.engine", "value": "решила перейти на PostgreSQL для проекта", "ts": 1700000000.0}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(p)


def _jsonl_file(tmp_path: Any) -> str:
    p = tmp_path / "chat.jsonl"
    p.write_text(
        json.dumps({"text": "я решила перейти на PostgreSQL для проекта"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"role": "user", "content": "наблюдение: трафик растёт по пятницам стабильно"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return str(p)


async def test_import_all_four_formats_end_to_end(import_db, tmp_path) -> None:
    from features.replay import replay
    from scripts.import_chat import import_records

    files = {
        "claude": _claude_file(tmp_path),
        "chatgpt": _chatgpt_file(tmp_path),
        "memory-json": _memory_json_file(tmp_path),
        "jsonl": _jsonl_file(tmp_path),
    }
    expected = {"claude": 2, "chatgpt": 2, "memory-json": 1, "jsonl": 2}  # system-role и пустой node скипаются

    for source, path in files.items():
        res = await import_records(source, path, "u1")
        assert res["captured"] == expected[source], source

    rows = await _l0_rows()
    assert len(rows) == sum(expected.values())
    assert all(r["event"] == "import" for r in rows)
    assert all(r["raw_type"] == "import" for r in rows)  # import не детерминирован — classify_raw не нужен
    assert all('"gate": "import"' in r["decisions"] for r in rows)
    # direct distill отработал: статус проставлен, инварианты легли в L4
    assert all(r["status"] in {"promoted_l4", "saved_l3"} for r in rows)
    conn = await connection_manager.get("memory.db")
    l4 = await (await conn.execute("SELECT COUNT(*) FROM core_memory WHERE user_id='u1'")).fetchone()
    assert int(l4[0]) >= 1

    # watermark-совместимость: replay не переобрабатывает import-строки
    assert await replay(since_days=1) == {"processed": 0, "skipped": 0, "conflicts": 0}


async def test_chatgpt_memory_json_normalization(import_db, tmp_path) -> None:
    from scripts.import_chat import parse_chatgpt, parse_memory_json

    recs = parse_chatgpt(json.loads(Path(_chatgpt_file(tmp_path)).read_text(encoding="utf-8")))
    assert [r["role"] for r in recs] == ["user", "assistant"]
    assert "PostgreSQL" in recs[0]["text"]

    recs = parse_memory_json(json.loads(Path(_memory_json_file(tmp_path)).read_text(encoding="utf-8")))
    assert recs[0]["text"] == "db.engine: решила перейти на PostgreSQL для проекта"
    assert recs[0]["ts"] == 1700000000.0


async def test_dry_run_writes_nothing(import_db, tmp_path) -> None:
    from scripts.import_chat import import_records

    res = await import_records("claude", _claude_file(tmp_path), "u1", dry_run=True)
    assert res["captured"] == 2
    assert await _l0_rows() == []  # ничего не записано
    conn = await connection_manager.get("memory.db")
    n = await (await conn.execute("SELECT COUNT(*) FROM core_memory")).fetchone()
    assert int(n[0]) == 0


async def test_unknown_source_rejected(import_db, tmp_path) -> None:
    from scripts.import_chat import import_records

    with pytest.raises(ValueError, match="unknown source"):
        await import_records("slack", _claude_file(tmp_path), "u1")
