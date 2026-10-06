"""Wire-тест: tier_l0 должен стоять в ночной цепочке backup_cron (S6, живой вызов)."""

from pathlib import Path


def test_tier_l0_wired_into_nightly_hooks() -> None:
    src = Path("features/backup_cron.py").read_text(encoding="utf-8")
    assert "from lifecycle.l0_tiers import tier_l0" in src
    assert "tier_l0()" in src  # вызов в _fire_nightly_hooks, а не только импорт
    assert "L0 tiering" in src  # результат попадает в отчёт


def test_backupcron_defaults_to_instance_data_dir(monkeypatch, tmp_path) -> None:
    """BackupCron() без явного base_dir наследует data-dir инстанса (connection_manager).

    Жёсткий дефолт ~/.mcp-ariel-memory заставлял все три живых MCP-инстанса
    (cowagent/hermes/mimocode) бэкапить одну мёртвую дефолтную БД вместо своих.
    """
    from shared.connection import AsyncConnectionManager
    import features.backup_cron as bc_mod
    from shared import connection as conn_mod

    fake_cm = AsyncConnectionManager(base_dir=str(tmp_path))
    monkeypatch.setattr(conn_mod, "connection_manager", fake_cm)
    cron = bc_mod.BackupCron()
    assert cron.base_dir == tmp_path
    assert cron.backup_dir == tmp_path / "backups"


def test_nightly_layer_budget_is_applied_to_the_hook_call() -> None:
    """Ночной хук слоя должен вызываться с ЯВНЫМ бюджетом NIGHTLY_LAYER_BUDGET_S.

    Без явного аргумента берётся дефолт `_await_on_main_loop` (120 с), и он же
    делится со всеми остальными шагами обслуживания. Замер на снимках живых баз:
    слой `user` у hermes стоит 138 с при всех тёплых кэшах, то есть при 120 с
    проход обрывался посередине слоя, а слой `agent` пропускался целиком.
    """
    from pathlib import Path

    import features.backup_cron as bc

    src = Path("features/backup_cron.py").read_text(encoding="utf-8")
    assert "timeout=NIGHTLY_LAYER_BUDGET_S" in src, "ночной хук снова зовётся без явного бюджета"
    assert bc.NIGHTLY_LAYER_BUDGET_S > 120, "бюджет не поднят: 120 с слой hermes не проходит"

    # Поднят только бюджет слоя. Общий дефолт шага обслуживания остаётся 120 с:
    # его делят между собой бэкап, вики-синк, ретеншн и остальные задачи, и
    # расширять им окно заодно с ночным проходом нельзя.
    import inspect

    default = inspect.signature(bc.BackupCron._await_on_main_loop).parameters["timeout"].default
    assert default == 120, f"общий дефолт шага обслуживания поехал: {default}"
