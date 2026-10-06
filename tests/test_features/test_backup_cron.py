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


# ── отвязка ночного прохода от 24-часового окна бэкапа (06.10) ──


def _cron(tmp_path, *, last_nightly_age_h: float | None):
    """BackupCron на изолированной базе + своё cycles_state.json."""
    import json as _json
    import time as _time

    import features.backup_cron as bc

    cron = bc.BackupCron(base_dir=str(tmp_path))
    if last_nightly_age_h is not None:
        (tmp_path / "cycles_state.json").write_text(
            _json.dumps({"last_nightly": _time.time() - last_nightly_age_h * 3600, "calls": []}),
            encoding="utf-8",
        )
    return cron


def test_nightly_runs_even_when_the_next_backup_is_a_day_away(tmp_path, monkeypatch) -> None:
    """Главное: проход идёт по своему расписанию, а не внутри ветки бэкапа.

    До правки единственный путь к ночному проходу лежал внутри `_check_backup`,
    а тот выходит, пока не прошли сутки с прошлого бэкапа, и `_do_backup`
    отмечает `_last_backup` ДО хуков. Погибший проход поэтому ждал ровно сутки.
    Проверка: бэкап только что был (`_check_backup` ничего не делает), а проход
    всё равно запускается.
    """
    import time as _time

    cron = _cron(tmp_path, last_nightly_age_h=48)
    cron._last_backup = _time.time()  # бэкап только что создан

    called: list[tuple] = []
    monkeypatch.setattr(cron, "_fire_nightly_hooks", lambda *a, **k: (called.append((a, k)), "ok")[1])

    cron._check_backup(_time.time())
    assert called == [], "свежий бэкап не должен запускать проход — иначе проверка бессмысленна"

    cron._check_nightly(_time.time())
    assert called, "проход обязан запускаться, не дожидаясь следующего бэкапа (сутки!)"


def test_not_due_cycle_runs_nothing_and_charges_no_backoff(tmp_path, monkeypatch) -> None:
    """Гейт остаётся главным: не созрело — не запускаем и бэкофф не начисляем."""
    import time as _time

    cron = _cron(tmp_path, last_nightly_age_h=1)  # 24 ч не прошло
    called: list[int] = []
    monkeypatch.setattr(cron, "_fire_nightly_hooks", lambda *a, **k: (called.append(1), "ok")[1])

    cron._check_nightly(_time.time())
    assert called == [], "незрелый цикл не запускается"
    assert cron._nightly_failures == 0
    assert cron._nightly_retry_at == 0.0


def test_failed_nightly_backs_off_and_the_pause_grows(tmp_path, monkeypatch) -> None:
    """Сбой не ждёт суток: повтор через бэкофф, и пауза растёт 15м → 1ч → 4ч."""
    import time as _time

    import features.backup_cron as bc

    cron = _cron(tmp_path, last_nightly_age_h=48)
    monkeypatch.setattr(cron, "_fire_nightly_hooks", lambda *a, **k: "failed")
    t0 = _time.time()
    cron._check_nightly(t0)
    assert cron._nightly_failures == 1
    first_pause = cron._nightly_retry_at - t0
    assert first_pause >= bc.NIGHTLY_BACKOFF_S[0] - 1, "первая пауза должна быть 15 мин, а не сутки"

    attempts: list[int] = []
    monkeypatch.setattr(cron, "_fire_nightly_hooks", lambda *a, **k: (attempts.append(1), "failed")[1])
    cron._check_nightly(t0 + 60)
    assert attempts == [], "внутри паузы повтор запрещён"

    cron._check_nightly(t0 + first_pause + 1)
    assert len(attempts) == 1, "после паузы повтор должен состояться"
    assert cron._nightly_failures == 2
    second_pause = cron._nightly_retry_at - _time.time()
    assert second_pause >= bc.NIGHTLY_BACKOFF_S[1] - 1, "вторая пауза должна вырасти до 1 ч"

    # Пауза упирается в потолок и дальше не растёт.
    assert bc.NIGHTLY_BACKOFF_S[-1] == 14400.0


def test_successful_nightly_clears_the_backoff(tmp_path, monkeypatch) -> None:
    """Успех снимает бэкофф: иначе один сбой тормозил бы проход до конца суток."""
    import time as _time

    cron = _cron(tmp_path, last_nightly_age_h=48)
    monkeypatch.setattr(cron, "_fire_nightly_hooks", lambda *a, **k: "failed")
    cron._check_nightly(_time.time())
    assert cron._nightly_failures == 1

    monkeypatch.setattr(cron, "_fire_nightly_hooks", lambda *a, **k: "ok")
    cron._check_nightly(cron._nightly_retry_at + 1)
    assert cron._nightly_failures == 0
    assert cron._nightly_retry_at == 0.0


def test_nightly_does_not_stack_on_an_unfinished_previous_pass(tmp_path, monkeypatch) -> None:
    """«Чтобы система не висла»: таймаут НЕ отменяет корутину, она живёт дальше.

    `asyncio.run_coroutine_threadsafe(...).result(timeout=...)` бросает исключение,
    но оставляет корутину работать на главном цикле — том самом, на котором живёт
    весь агент. Второй проход поверх незавершённого занял бы цикл целиком, поэтому
    незавершённая работа обязана блокировать повтор.
    """
    import concurrent.futures
    import time as _time

    cron = _cron(tmp_path, last_nightly_age_h=48)
    cron._pending_futures.append(concurrent.futures.Future())  # не завершён
    cron._nightly_started_at = _time.time()

    called: list[int] = []
    monkeypatch.setattr(cron, "_fire_nightly_hooks", lambda *a, **k: (called.append(1), "ok")[1])
    cron._check_nightly(_time.time())

    assert called == [], "второй проход не должен идти поверх незавершённого"
    assert cron._nightly_failures == 0, "ожидание чужой работы — не сбой, бэкофф не начисляется"


def test_finished_pass_stops_blocking_retries(tmp_path, monkeypatch) -> None:
    """Как только брошенная корутина завершилась, повтор снова разрешён."""
    import concurrent.futures
    import time as _time

    cron = _cron(tmp_path, last_nightly_age_h=48)
    done = concurrent.futures.Future()
    done.set_result(None)
    cron._pending_futures.append(done)
    cron._nightly_started_at = _time.time()

    called: list[int] = []
    monkeypatch.setattr(cron, "_fire_nightly_hooks", lambda *a, **k: (called.append(1), "ok")[1])
    cron._check_nightly(_time.time())
    assert called, "завершённая работа не должна блокировать проход"


def test_lost_pass_is_released_after_the_grace_period(tmp_path, monkeypatch) -> None:
    """Вечно незавершённая корутина не должна заблокировать проход НАВСЕГДА.

    Иначе лечение одной болезни («прохода не будет никогда») стало бы другой.
    """
    import concurrent.futures
    import time as _time

    import features.backup_cron as bc

    cron = _cron(tmp_path, last_nightly_age_h=48)
    cron._pending_futures.append(concurrent.futures.Future())  # не завершится никогда
    cron._nightly_started_at = _time.time() - bc.NIGHTLY_INFLIGHT_GRACE_S - 1

    called: list[int] = []
    monkeypatch.setattr(cron, "_fire_nightly_hooks", lambda *a, **k: (called.append(1), "ok")[1])
    cron._check_nightly(_time.time())
    assert called, "потерянный проход обязан быть отпущен по истечении grace-периода"


def test_not_being_the_lock_leader_is_not_a_failure(tmp_path, monkeypatch) -> None:
    """Проигравший гонку писатель не начисляет бэкофф: работу делает лидер."""
    import time as _time

    cron = _cron(tmp_path, last_nightly_age_h=48)
    monkeypatch.setattr(cron, "_acquire_backup_lock", lambda: None)

    called: list[int] = []
    monkeypatch.setattr(cron, "_fire_nightly_hooks", lambda *a, **k: (called.append(1), "ok")[1])
    cron._check_nightly(_time.time())

    assert called == []
    assert cron._nightly_failures == 0, "чужой лидер — не сбой"
    assert cron._nightly_retry_at == 0.0


def test_nightly_backoff_survives_a_restart(tmp_path, monkeypatch) -> None:
    """Перезапуски тут рутина (у hermes 10 за 06.10): бэкофф не обнуляется на каждом.

    Иначе будний день с десятью перезапусками превращался бы в десять попыток
    138-секундного прохода вместо одной.
    """
    import time as _time

    import features.backup_cron as bc

    cron = _cron(tmp_path, last_nightly_age_h=48)
    monkeypatch.setattr(cron, "_fire_nightly_hooks", lambda *a, **k: "failed")
    cron._check_nightly(_time.time())
    assert cron._nightly_failures == 1

    restarted = bc.BackupCron(base_dir=str(tmp_path))
    assert restarted._nightly_failures == 1, "счётчик сбоев должен пережить перезапуск"
    assert restarted._nightly_retry_at > _time.time(), "пауза должна пережить перезапуск"

    called: list[int] = []
    monkeypatch.setattr(restarted, "_fire_nightly_hooks", lambda *a, **k: (called.append(1), "ok")[1])
    restarted._check_nightly(_time.time())
    assert called == [], "после перезапуска пауза обязана ещё действовать"


def test_failed_layer_does_not_record_the_cycle_as_done(tmp_path, monkeypatch) -> None:
    """Слой упал → цикл НЕ отмечается выполненным, и проход рапортует 'failed'.

    Именно так `cycles_state.last_nightly` у Люси замер на 04.10 08:54: запись
    идёт после обоих слоёв, поэтому обрыв её не оставлял.
    """
    import json as _json
    import time as _time

    import features.backup_cron as bc

    due = _time.time() - 48 * 3600
    state = tmp_path / "cycles_state.json"
    state.write_text(_json.dumps({"last_nightly": due, "calls": []}), encoding="utf-8")

    class _Reg:
        @staticmethod
        def fire(name, layer, ctx, mem=None, graph=None):
            raise TimeoutError("слой не уложился в бюджет")

    monkeypatch.setattr("hooks.registry.hook_registry", _Reg())

    cron = bc.BackupCron(base_dir=str(tmp_path))
    # Корутины не гоняем, но и не бросаем: незакрытая корутина даёт RuntimeWarning
    # и маскирует настоящие предупреждения в отчёте.
    cron._await_on_main_loop = lambda coro, **kwargs: (coro.close(), {})[1]

    assert cron._fire_nightly_hooks(state) == "failed"
    assert _json.loads(state.read_text())["last_nightly"] == due, "упавший проход не должен закрывать цикл"
