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

    Именно так `cycles_state.last_nightly` замер на 04.10 08:54: запись
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


def test_expired_budget_with_live_work_is_not_a_failure(tmp_path, monkeypatch) -> None:
    """Таймаут ожидания — не провал, когда работа продолжается на main loop.

    `_await_on_main_loop(...).result(timeout=)` бросает, но НЕ отменяет корутину
    (см. комментарий у NIGHTLY_INFLIGHT_GRACE_S). Считать это провалом замерено
    на живой базе 14.10.2026:

        14:37:41 layer=user starting
        14:40:41 finished in 180.0 s     <- бюджет истёк
        14:44:46 graph_enrich 425773 ms  <- работа ТЯНУЛАСЬ дальше
        14:57:41 layer=user starting     <- ВТОРОЙ проход, т.к. цикл не закрыт
        15:00:41 finished in 180.0 s     <- и снова бюджет истёк

    Двойная работа поверх одной базы — это и есть источник `database is locked`,
    от которого страдает в том числе `migrate()` при старте сессии.
    """
    import json as _json
    import time as _time
    import concurrent.futures

    import features.backup_cron as bc

    due = _time.time() - 48 * 3600
    state = tmp_path / "cycles_state.json"
    state.write_text(_json.dumps({"last_nightly": due, "calls": []}), encoding="utf-8")

    cron = bc.BackupCron(base_dir=str(tmp_path))

    def _expire(coro, **kwargs):
        coro.close()
        # Работа осталась на main loop: именно это отличает таймаут от падения.
        cron._pending_futures.append(concurrent.futures.Future())
        raise TimeoutError("бюджет слоя истёк")

    cron._await_on_main_loop = _expire

    assert cron._fire_nightly_hooks(state) == "running", (
        "истёкшее ожидание при живой работе — это 'running', не 'failed': провал заставил бы повторить уже идущий проход"
    )


def test_timed_out_pass_charges_no_backoff(tmp_path, monkeypatch) -> None:
    """За 'running' не начисляется бэкофф: повтор не нужен, работа и так идёт.

    Пока проход жив, `_nightly_busy` и так откажет в новом запуске. Бэкофф вдобавок
    к этому лишь откладывает следующий нормальный проход на 15/60/240 минут.
    """
    import time as _time

    cron = _cron(tmp_path, last_nightly_age_h=48)
    monkeypatch.setattr(cron, "_fire_nightly_hooks", lambda *a, **k: "running")
    cron._check_nightly(_time.time())

    assert cron._nightly_failures == 0, "идущий проход — не сбой"
    assert cron._nightly_retry_at == 0.0, "повтор не нужен: работа уже идёт"


def test_budget_covers_the_measured_nightly_layer() -> None:
    """Бюджет слоя выведен из замера, а не подобран 'побольше'.

    `user graph_enrich` по 12 замерам живой базы hermes (14.10.2026):
    p50 151 с · p90 236 с · p95 324 с · max 426 с. При 180 с не укладывалось
    4 прохода из 12 — и каждый такой провал порождал ВТОРОЙ проход поверх
    первого (см. `test_expired_budget_with_live_work_is_not_a_failure`).

    Бюджет обязан покрывать наблюдавшийся максимум с запасом: следующий замер,
    превысивший бюджет, снова запустит двойную работу.
    """
    import features.backup_cron as bc

    assert bc.NIGHTLY_LAYER_BUDGET_S >= 480, (
        f"бюджет {bc.NIGHTLY_LAYER_BUDGET_S} с не покрывает замеренный максимум 426 с с запасом — истёкшее ожидание порождает повторный проход"
    )
    # Общий дефолт шага обслуживания не трогаем: его делят бэкап, вики-синк и прочее.
    import inspect

    default = inspect.signature(bc.BackupCron._await_on_main_loop).parameters["timeout"].default
    assert default == 120, "общий дефолт обслуживания остаётся 120 с"


def _hold_maintenance(base_dir):
    """Другой держатель того же замка обслуживания, как отдельный процесс."""
    import fcntl

    from shared.maintenance_lock import LOCK_NAME

    fh = open(base_dir / LOCK_NAME, "w")  # noqa: SIM115 — закрытие снимет flock
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


def _free_maintenance(base_dir) -> bool:
    """Свободен ли замок обслуживания прямо сейчас."""
    from shared import maintenance_lock

    fd = maintenance_lock.acquire(base_dir)
    if fd is None:
        return False
    maintenance_lock.release(fd)
    return True


def test_nightly_takes_the_maintenance_lock_around_its_work(tmp_path, monkeypatch) -> None:
    """Между ночным проходом и 15-минутной уборкой должно быть взаимное исключение.

    Замерено на живой базе 14.10.2026: два РАЗНЫХ замка на одну базу означали,
    что они не видят друг друга как занятые:

        .backup_cron.lock   ← ночной проход и бэкап
        .maintenance.lock   ← 15-минутная уборка и планировщик

    В 14:44:46 `graph_enrich` ещё писал в базу, а `consolidate_episodes` полез
    туда же и получил `database is locked`. Один писатель на базу — а между
    разными видами работы сериализации не было.
    """
    import json as _json
    import time as _time

    import features.backup_cron as bc

    due = _time.time() - 48 * 3600
    state = tmp_path / "cycles_state.json"
    state.write_text(_json.dumps({"last_nightly": due, "calls": []}), encoding="utf-8")

    held_during: list[bool] = []

    async def _noop() -> None:
        return None

    class _Reg:
        @staticmethod
        def fire(name, layer, ctx, mem=None, graph=None):
            # Пока идёт работа, замок обслуживания обязан быть занят.
            held_during.append(not _free_maintenance(tmp_path))
            return _noop()

    monkeypatch.setattr("hooks.registry.hook_registry", _Reg())

    cron = bc.BackupCron(base_dir=str(tmp_path))
    cron._await_on_main_loop = lambda coro, **kwargs: (coro.close(), {})[1]

    assert cron._fire_nightly_hooks(state) == "ok"
    assert held_during, "хуки не вызвались — тест ничего не проверил"
    assert all(held_during), "во время ночной работы замок обслуживания был свободен"
    assert _free_maintenance(tmp_path), "после прохода замок обязан быть отдан"


def test_nightly_does_not_write_while_housekeeping_holds_the_base(tmp_path, monkeypatch) -> None:
    """Занято чужим держателем → ночной проход не пишет в базу вообще.

    Это и есть та половина, ради которой правка делалась: уборка держит базу
    секунды, и ночной проход обязан подождать её, а не полезть писать поверх.
    """
    import json as _json
    import time as _time

    import features.backup_cron as bc

    due = _time.time() - 48 * 3600
    state = tmp_path / "cycles_state.json"
    state.write_text(_json.dumps({"last_nightly": due, "calls": []}), encoding="utf-8")

    fired: list[int] = []

    async def _noop() -> None:
        return None

    class _Reg:
        @staticmethod
        def fire(name, layer, ctx, mem=None, graph=None):
            fired.append(1)
            return _noop()

    monkeypatch.setattr("hooks.registry.hook_registry", _Reg())
    # Ждать нечего: держатель не отдаст замок в пределах окна ожидания.
    monkeypatch.setattr(bc, "NIGHTLY_MAINTENANCE_WAIT_S", 0.2)

    cron = bc.BackupCron(base_dir=str(tmp_path))
    cron._await_on_main_loop = lambda coro, **kwargs: (coro.close(), {})[1]

    held = _hold_maintenance(tmp_path)
    try:
        verdict = cron._fire_nightly_hooks(state)
    finally:
        import fcntl

        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()

    assert fired == [], "ночная работа пошла, пока базу держал другой писатель"
    assert verdict == "skipped", f"вердикт должен быть 'skipped', а не {verdict!r}"
    assert _json.loads(state.read_text())["last_nightly"] == due, "цикл не должен закрыться"


def test_nightly_keeps_the_lock_while_abandoned_work_finishes(tmp_path, monkeypatch) -> None:
    """Истёкший бюджет не отдаёт замок: работа ещё идёт, и уборке тут не место.

    Именно это столкновение и замерено — в 14:44:46 проход уже вышел за бюджет
    (`'running'`), но продолжал писать, а уборка в тот же момент лезла в базу.
    Отдать замок вместе с ожиданием — значит не починить ничего.
    """
    import concurrent.futures
    import json as _json
    import time as _time

    import features.backup_cron as bc

    due = _time.time() - 48 * 3600
    state = tmp_path / "cycles_state.json"
    state.write_text(_json.dumps({"last_nightly": due, "calls": []}), encoding="utf-8")

    cron = bc.BackupCron(base_dir=str(tmp_path))
    live = concurrent.futures.Future()

    def _expire(coro, **kwargs):
        coro.close()
        cron._pending_futures.append(live)
        raise TimeoutError("бюджет слоя истёк")

    cron._await_on_main_loop = _expire

    assert cron._fire_nightly_hooks(state) == "running"
    assert not _free_maintenance(tmp_path), "замок отдан, пока брошенная работа ещё пишет — ровно то столкновение, которое чиним"

    live.set_result(None)  # работа завершилась
    assert _free_maintenance(tmp_path), "после завершения работы замок обязан вернуться"


def test_stuck_work_does_not_hold_the_lock_forever(tmp_path, monkeypatch) -> None:
    """Отказ от потерянной работы отдаёт и замок.

    `_nightly_busy` по грейс-периоду признаёт работу потерянной и чистит список.
    Замок обязан уйти вместе с ней, иначе уборка не пройдёт до перезапуска.
    """
    import concurrent.futures
    import time as _time

    import features.backup_cron as bc

    cron = _cron(tmp_path, last_nightly_age_h=48)
    cron._pending_futures.append(concurrent.futures.Future())  # никогда не завершится
    cron._nightly_started_at = _time.time() - bc.NIGHTLY_INFLIGHT_GRACE_S - 60

    # Замок в руках у прохода, который мы сейчас признаем потерянным.
    from shared import maintenance_lock

    fd = maintenance_lock.acquire(tmp_path)
    assert fd is not None
    cron._nightly_lock_fd = fd

    assert cron._nightly_busy(_time.time()) is False, "потерянная работа должна перестать блокировать"
    assert _free_maintenance(tmp_path), "замок не отдан вместе с потерянной работой"


def test_a_skipped_pass_does_not_clear_the_failure_ladder(tmp_path, monkeypatch) -> None:
    """Пропуск из-за занятого замка — не успех, и не повод обнулить бэкофф.

    Иначе проход, падающий за спиной занятой базы, повторялся бы каждые 15 минут
    вечно вместо отступления на 1 ч / 4 ч — а база занята как раз тогда, когда
    повтор меньше всего нужен.
    """
    import time as _time

    cron = _cron(tmp_path, last_nightly_age_h=48)
    cron._nightly_failures = 2
    cron._nightly_retry_at = 0.0
    monkeypatch.setattr(cron, "_fire_nightly_hooks", lambda *a, **k: "skipped")

    cron._check_nightly(_time.time())

    assert cron._nightly_failures == 2, "пропуск обнулил лестницу сбоев"
    assert cron._nightly_retry_at == 0.0, "за пропуск бэкофф не начисляется"
