"""T14 (аудит 05.09, P0-6): мёртвые модули ожили — wire-тесты.

- cycles: nightly_gate (cycle_due + budget) + record_nightly_done → backup_cron._fire_nightly_hooks
- middleware: default_pipeline защищает dispatch_event (rate-limit/dedup/audit)
- typed_export: subcommand `export-typed` в ariel_cli
- wiki_communities: detect_communities в отчёте graph_enrich
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


# ── cycles gate ──


def test_nightly_gate_first_run_is_due(tmp_path):
    from features.cycles import nightly_gate

    gate = nightly_gate(tmp_path / "state.json")
    assert gate["action"] == "run"


def test_nightly_gate_recent_run_skips(tmp_path):
    from features.cycles import nightly_gate, record_nightly_done

    state = tmp_path / "state.json"
    record_nightly_done(state, now=1000.0)
    gate = nightly_gate(state, now=1000.0 + 60)  # 1 минута спустя — цикл не созрел
    assert gate["action"] == "skip"
    assert gate["reason"] == "cycle_not_due"


def test_nightly_gate_budget_throttle(tmp_path):
    """rolling-переполнение → budget=throttle (мягкий вердикт, проход идёт);
    жёсткий block в gate недостижим по построению (used=0, per_task=0) —
    он для будущих LLM-циклов с реальным used_this_cycle."""
    from features.cycles import CycleBudget, nightly_gate, record_nightly_done

    state = tmp_path / "state.json"
    record_nightly_done(state, now=1000.0)  # last_run в прошлом → cycle_due
    data = json.loads(state.read_text())
    data["calls"] = [90000.0 + i for i in range(300)]  # 300 вызовов в rolling-окне от now=91000
    state.write_text(json.dumps(data))
    gate = nightly_gate(state, now=1000.0 + 25 * 3600, budget=CycleBudget(max_rolling_60m=200))
    assert gate["action"] == "run" and gate["budget"] == "throttle"


@pytest.mark.asyncio
async def test_backup_cron_respects_cycles_gate(cm, monkeypatch):
    """cycle не созрел → nightly-хуки не гоняются; созрел → гоняются и last_run пишется."""
    from features.backup_cron import BackupCron

    fired = []

    class _Reg:
        @staticmethod
        def fire(name, layer, ctx, mem=None, graph=None):
            fired.append((name, layer))
            return {"results": []}  # sync: cron._await_on_main_loop не исполняет coroutine

    monkeypatch.setattr("hooks.registry.hook_registry", _Reg())

    state = cm.base_dir / "cycles_state.json"
    state.write_text(json.dumps({"last_nightly": 1000.0}))

    cron = BackupCron()
    cron._await_on_main_loop = lambda coro: coro  # корутины не гоняем, только факт вызова

    # цикл не созрел (24ч не прошло; cron зовёт nightly_gate без now → патчим время cycles)
    from features import cycles as cycles_mod

    real_time = cycles_mod.time.time
    monkeypatch.setattr(cycles_mod.time, "time", lambda: 1600.0)
    cron._fire_nightly_hooks()
    assert fired == [], "свежий last_run → nightly пропущен"

    # цикл созрел
    monkeypatch.setattr(cycles_mod.time, "time", lambda: 1000.0 + 25 * 3600)
    cron._fire_nightly_hooks()
    assert fired, "созревший цикл гоняет nightly"
    assert json.loads(state.read_text())["last_nightly"] > 1000.0, "last_run зафиксирован"
    monkeypatch.setattr(cycles_mod.time, "time", real_time)


# ── middleware на dispatch_event ──


@pytest.mark.asyncio
async def test_middleware_pipeline_guards_dispatch_event(cm, monkeypatch):
    """Дедуп-middleware: два одинаковых события подряд → второй deduped."""
    fired = []

    async def _handler(event, layer, ctx, mem=None, graph=None):
        fired.append(ctx.get("text", ""))
        return {"results": [{"ok": True}]}

    monkeypatch.setattr("hooks.registry.hook_registry", type("_R", (), {"fire": staticmethod(_handler)})())

    from hooks.external import dispatch_event

    kwargs = {
        "event": "session_ended",
        "layer": "user",
        "user_id": "mw",
        "payload": {"text": "одинаковое сообщение для дедупа"},
        "mem": MagicMock(),
        "graph": MagicMock(),
    }
    r1 = await dispatch_event(**kwargs)
    r2 = await dispatch_event(**kwargs)
    assert not r1.get("skipped") and r1.get("status") != "deduped", f"первое событие проходит: {r1}"
    assert r2.get("status") == "deduped", f"повтор дедупится middleware'ом: {r2}"
    assert len(fired) == 1, "хендлер вызван один раз"


# ── typed_export в CLI ──


@pytest.mark.asyncio
async def test_typed_export_writes_file(cm):
    from features.typed_export import do_export

    path = await do_export("u9", None)
    assert Path(path).exists(), "do_export пишет файл в base_dir/exports"


def test_typed_export_cli_subcommand(tmp_path, monkeypatch, capsys):
    """sync-тест: CLI-обёртка (asyncio.run) вызывается вне event loop."""
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()

    import asyncio

    from shared.migrations import MigrationManager

    asyncio.run(MigrationManager(cm=connection_manager).migrate())

    import argparse

    from scripts.ariel_cli import _cmd_export_typed

    rc = _cmd_export_typed(argparse.Namespace(user="u9", kind=None))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert Path(out).exists()
    connection_manager._conns.clear()


# ── wiki_communities в graph_enrich ──


@pytest.mark.asyncio
async def test_graph_enrich_reports_wiki_communities(cm):
    """detect_communities вызывается ночным проходом (A1.6 wire)."""
    from lifecycle.graph_enrich import graph_enrich

    res = await graph_enrich(layer="user")
    assert "wiki_communities" in res, "ключ отчёта присутствует"
    assert isinstance(res["wiki_communities"], list)
