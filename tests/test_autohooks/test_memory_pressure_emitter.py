"""E16: memory_pressure emitter in the autohooks daemon (L1 growth + hysteresis)."""

import asyncio
from types import SimpleNamespace


class _Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event, layer, user_id, payload, mem, graph, rag):
        self.events.append((event, payload))
        return {}


def _cfg(tmp_path):
    from autohooks.config import AgentConfig, SourceConfig

    src = SourceConfig(
        driver="sqlite",
        path=tmp_path / "src.db",
        table="messages",
        cursor_column="id",
        order_by="id",
        role=SimpleNamespace(column="role", json_path=None),
        text=SimpleNamespace(column="content", json_path=None),
    )
    return AgentConfig(
        layer="user",
        user_id="default",
        data_dir=tmp_path,
        source=src,
        state_file=tmp_path / "cursor.json",
        poll_seconds=0.01,
        batch_limit=10,
    )


def _mem(l1_size):
    return SimpleNamespace(l1=SimpleNamespace(get_full=lambda: [object()] * l1_size))


class _EmptySource:
    """Source that yields one empty batch per fetch (daemon idles)."""

    def __init__(self):
        self.calls = 0

    def fetch_after(self, cursor, limit):
        self.calls += 1
        return SimpleNamespace(messages=[], cursor=cursor)

    def max_id(self):
        return 0

    def close(self):
        pass


def _run_daemon_once(cfg, mem, dispatch, source=None):
    from autohooks.daemon import run_daemon

    async def _main():
        await run_daemon(cfg, source or _EmptySource(), mem, None, None, max_iterations=1, dispatch=dispatch, poll=lambda *_: asyncio.sleep(0))

    asyncio.run(_main())


def test_emits_once_when_crossing_threshold(tmp_path):
    cfg = _cfg(tmp_path)
    mem = _mem(45)
    rec = _Recorder()
    _run_daemon_once(cfg, mem, rec)
    # one iteration dispatches new_message nothing, but pressure fires (45 > 40, 45-0 >= 10)
    assert ("memory_pressure", {"l1_size": 45}) in rec.events


def test_hysteresis_no_redispatch_same_size(tmp_path):
    """Second daemon run at the SAME size must not re-dispatch (state persists via cursor file? No — in-memory).

    The emitter's hysteresis is per-process; each run_daemon starts fresh, so
    within ONE run two iterations at the same size fire once.
    """
    from autohooks.daemon import run_daemon

    cfg = _cfg(tmp_path)
    mem = _mem(45)
    rec = _Recorder()
    calls = {"n": 0}

    class _TwoBatchSource(_EmptySource):
        def fetch_after(self, cursor, limit):
            calls["n"] += 1
            return SimpleNamespace(messages=[], cursor=cursor)

    async def _main():
        await run_daemon(cfg, _TwoBatchSource(), mem, None, None, max_iterations=2, dispatch=rec, poll=lambda *_: asyncio.sleep(0))

    asyncio.run(_main())
    pressures = [e for e in rec.events if e[0] == "memory_pressure"]
    assert len(pressures) == 1  # second iteration at same size: 45-45=0 < 10


def test_no_emission_below_threshold(tmp_path):
    cfg = _cfg(tmp_path)
    mem = _mem(30)
    rec = _Recorder()
    _run_daemon_once(cfg, mem, rec)
    assert not any(e[0] == "memory_pressure" for e in rec.events)


def test_no_emission_without_l1(tmp_path):
    cfg = _cfg(tmp_path)
    rec = _Recorder()
    _run_daemon_once(cfg, SimpleNamespace(), rec)  # mem without .l1
    assert not any(e[0] == "memory_pressure" for e in rec.events)
