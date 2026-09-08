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
