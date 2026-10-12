"""purge_speaker_layer_leaks: the plan selects the leak, and nothing else.

The failure mode this guards is the one the script itself was written to fix:
selecting by CONTENT instead of by the layer. A first draft matched episodes on
the `raw:<l0_id>` tag alone and swept all 6030 legitimate agent episodes.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager


@pytest.fixture
async def db(tmp_path):
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path  # НЕ подменять объект!
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()
    connection_manager.base_dir = original  # восстановить: stale tmp-dir травит no-db тесты


async def _l0(layer: str, text: str) -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO l0_journal (ts, event, layer, user_id, text, raw_type, status, decisions)"
        " VALUES (1.0, 'new_message', ?, 'default', ?, 'user-message', 'received', '[]')",
        (layer, text),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def _fact(layer: str, key: str, value: str, raw_id: int | None) -> int:
    conn = await connection_manager.get(DB_NAME)
    meta = json.dumps({"source_raw_id": raw_id}) if raw_id is not None else None
    cur = await conn.execute(
        "INSERT INTO core_memory (user_id, key, value, importance, memory_kind, layer, source, metadata,"
        " created_at, updated_at)"
        " VALUES ('default', ?, ?, 0.5, 'rule', ?, 'new_message', ?, 1.0, 1.0)",
        (key, value, layer, meta),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def _episode(layer: str, summary: str, raw_id: int) -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO episodes (user_id, summary, emotional_weight, tags, layer, created_at) VALUES ('default', ?, 0.5, ?, ?, 1.0)",
        (summary, json.dumps([f"raw:{raw_id}"]), layer),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def test_selects_only_the_owner_row_whose_source_is_the_agent(db) -> None:
    """Слой говорящего — по источнику: agent-L0 в user-слое = утечка, user-L0 — нет."""
    from scripts.purge_speaker_layer_leaks import plan

    agent_l0 = await _l0("agent", "я нашла причину и правлю")
    user_l0 = await _l0("user", "а я хочу вот так")
    leak = await _fact("user", "rule:утекло", "моя реплика", agent_l0)
    legit = await _fact("user", "rule:законно", "её слова", user_l0)
    await _fact("agent", "rule:на_месте", "уже в правильном слое", agent_l0)

    p = plan(connection_manager.base_dir / DB_NAME)
    ids = {int(r["entry_id"]) for r in p["facts"]}
    assert leak in ids, "agent-L0 в user-слое обязан попасть в план"
    assert legit not in ids, "user-L0 трогать нельзя"
    assert len(ids) == 1


async def test_does_not_sweep_legitimate_agent_episodes(db) -> None:
    """Регресс того самого бага: фильтр по слою обязателен.

    Без `WHERE layer='user'` выборка по одному raw-тегу забирает ВСЕ агентские
    эпизоды — на живой базе 6030 вместо 0.
    """
    from scripts.purge_speaker_layer_leaks import plan

    l0 = await _l0("agent", "текст агента")
    await _episode("agent", "законный агентский эпизод", l0)
    await _episode("agent", "ещё один законный", l0)
    await _episode("user", "а это утечка", l0)

    p = plan(connection_manager.base_dir / DB_NAME)
    assert len(p["episodes"]) == 1, "только user-эпизод с agent-источником"
    assert str(p["episodes"][0]["summary"]) == "а это утечка"


def _run_cli(repo: Path, env: dict, *argv: str):
    """Run the script out of process (sync: a blocking call must not sit in async def)."""
    import subprocess

    return subprocess.run(
        [sys.executable, str(repo / "scripts" / "purge_speaker_layer_leaks.py"), *argv],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


async def test_dry_run_is_the_default_and_writes_nothing(db, tmp_path) -> None:
    """Реальный CLI: запуск без `--apply` не пишет — ни удаления, ни архива.

    Через subprocess, а не вызовом main() в процессе: main() крутит свой цикл,
    и вызов из async-теста — вложенный цикл (RuntimeError), а не проверка.
    """
    import os
    from pathlib import Path

    agent_l0 = await _l0("agent", "реплика агента")
    await _fact("user", "rule:утекло", "моя реплика", agent_l0)

    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["MCP_MEMORY_DATA_DIR"] = str(tmp_path)
    env.pop("PYTHONPATH", None)  # чужой install-venv в PYTHONPATH роняет импорт

    proc = _run_cli(repo, env, "--dry-run")
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "DRY RUN" in proc.stdout

    conn = await connection_manager.get(DB_NAME)
    n = (await (await conn.execute("SELECT COUNT(*) FROM core_memory")).fetchone() or (0,))[0]
    archived = (await (await conn.execute("SELECT COUNT(*) FROM archived_memories")).fetchone() or (0,))[0]
    assert n == 1, "dry-run не удаляет"
    assert archived == 0, "dry-run не архивирует"


def _cli_env(tmp_path) -> dict:
    """Env for the out-of-process CLI run (no PYTHONPATH: чужой install-venv)."""
    import os

    env = dict(os.environ)
    env["MCP_MEMORY_DATA_DIR"] = str(tmp_path)
    env.pop("PYTHONPATH", None)
    return env


async def test_apply_writes_one_audit_row_per_removed_record(db, tmp_path) -> None:
    """Удаление оставляет след по каждой записи, а не одну сводку.

    `features/blame.py` спрашивает `WHERE target_id=?` — «что случилось с этой
    записью». Сводная строка на такой вопрос не отвечает, поэтому форма повторяет
    `purge_l3_legacy`: одна строка на запись, с id внутри `target_id`.
    """
    from pathlib import Path

    agent_l0 = await _l0("agent", "реплика агента")
    eid = await _fact("user", "rule:утекло", "моя реплика", agent_l0)
    ep_id = await _episode("user", "утекший эпизод", agent_l0)

    repo = Path(__file__).resolve().parents[2]
    proc = _run_cli(repo, _cli_env(tmp_path), "--apply")
    assert proc.returncode == 0, proc.stderr[-800:]

    conn = await connection_manager.get(DB_NAME)
    rows = await (await conn.execute("SELECT target_id, layer, action, details FROM audit_log WHERE action='speaker_layer_leak_purge'")).fetchall()
    audited = {str(r[0]) for r in rows}
    assert f"core_memory:{eid}" in audited, f"нет следа по удалённой записи: {audited}"
    assert f"episode:{ep_id}" in audited, f"нет следа по удалённому эпизоду: {audited}"
    assert all(str(r[1]) == "user" for r in rows), "слой берётся у удаляемой строки"
    assert all("speaker_layer_leak" in str(r[3]) for r in rows), "причина названа в details"


async def test_apply_removes_the_episodes_the_plan_reports(db, tmp_path) -> None:
    """Регресс: план и отчёт считали эпизоды, а apply их не удалял — молча.

    На живой базе эпизодов 0, поэтому несогласованность не проявлялась. При
    возвращении класса (replay) скрипт печатал бы успех поверх неудалённых строк.
    """
    from pathlib import Path

    agent_l0 = await _l0("agent", "реплика агента")
    await _fact("user", "rule:утекло", "моя реплика", agent_l0)
    await _episode("user", "утекший эпизод", agent_l0)

    repo = Path(__file__).resolve().parents[2]
    proc = _run_cli(repo, _cli_env(tmp_path), "--apply")
    assert proc.returncode == 0, proc.stderr[-800:]

    conn = await connection_manager.get(DB_NAME)
    left = (await (await conn.execute("SELECT COUNT(*) FROM episodes")).fetchone() or (0,))[0]
    assert left == 0, "эпизод из плана обязан быть удалён, а не только показан"
    archived = await (await conn.execute("SELECT COUNT(*) FROM archived_memories WHERE archive_reason=?", ("speaker_layer_leak",))).fetchone()
    assert int((archived or (0,))[0]) == 2, "заархивированы и запись, и эпизод"
