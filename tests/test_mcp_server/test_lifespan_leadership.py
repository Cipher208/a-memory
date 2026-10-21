"""Leadership loop: claim, promote once, retry at a slow cadence (spec S4)."""

from __future__ import annotations

import asyncio
import fcntl

from mcp_server.lifespan import leadership_loop
from shared.leader_lock import LOCK_NAME, claim, release


async def test_promotes_on_the_second_attempt() -> None:
    stop = asyncio.Event()
    outcomes = [None, 3]
    claims: list[int] = []
    promoted: list[int] = []

    def fake_claim() -> int | None:
        claims.append(1)
        return outcomes.pop(0) if outcomes else 3

    await leadership_loop(
        fake_claim,
        lambda fd: None,
        lambda: (promoted.append(1), stop.set()),
        stop,
        retry_s=0.01,
    )
    assert promoted == [1], "promote must run exactly once, after a won claim"
    assert len(claims) == 2, "a lost claim must be retried, not fatal"


async def test_follower_promotes_after_the_holder_leaves(tmp_path) -> None:
    # Real flock semantics: promotion ≤ retry cadence after the old leader's
    # process would have died (spec S2 — fcntl dies with the process, here
    # simulated by unlocking).
    stop = asyncio.Event()
    promoted = asyncio.Event()
    captured: list[int] = []
    holder = open(tmp_path / LOCK_NAME, "w")  # noqa: SIM115, ASYNC230

    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

    task = asyncio.create_task(leadership_loop(lambda: claim(tmp_path), captured.append, promoted.set, stop, retry_s=0.02))
    await asyncio.sleep(0.06)
    assert not promoted.is_set(), "must not promote while another leader holds the lock"

    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    holder.close()
    await asyncio.wait_for(promoted.wait(), timeout=1.0)

    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    # Leave .leader.lock released (Global Constraint): the loop never releases
    # the fd it claimed — lifespan's finally owns that — so the test must.
    assert len(captured) == 1, "on_claim must receive the promoted fd"
    release(captured[0])


async def test_stopped_loop_never_claims() -> None:
    calls: list[int] = []
    stop = asyncio.Event()
    stop.set()
    await leadership_loop(lambda: calls.append(1) or 1, lambda fd: None, lambda: None, stop)
    assert calls == [], "an instance shutting down must not grab leadership"


async def test_claim_error_retries_instead_of_dying() -> None:
    stop = asyncio.Event()
    promoted = asyncio.Event()
    outcomes: list[int | OSError] = [OSError("EMFILE"), 5]

    def flaky_claim() -> int | None:
        value = outcomes.pop(0) if outcomes else 5
        if isinstance(value, OSError):
            raise value
        return value

    await leadership_loop(
        flaky_claim,
        lambda fd: None,
        lambda: (promoted.set(), stop.set()),
        stop,
        retry_s=0.01,
    )
    assert promoted.is_set(), "a raising claim must be retried, not fatal"
