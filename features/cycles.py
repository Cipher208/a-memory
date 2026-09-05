"""C7 / S13: cycles-daemon config + chimera triple cost-cap.

CycleConfig — интервалы демона (dream 24ч, gap-reader 1ч, reminder 60м,
inactivity 3ч). CycleBudget + check_budget — тройной cost-cap для
LLM-трогающей цикловой работы: per-cycle (жёстко), rolling-60m (мягко,
восстанавливается само), per-task (жёстко). RollingCounter — deque
timestamp'ов в памяти (без персистентности: пережить рестарт не нужно).
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROLLING_WINDOW_S = 3600.0
THROTTLE_FRACTION = 0.9  # approaching a hard cap → soft throttle before the block


@dataclass
class CycleConfig:
    """S13 cycles-daemon intervals (план: тики 60s/1ч/3ч/24ч)."""

    dream_hours: float = 24.0
    gap_hours: float = 1.0
    reminder_minutes: float = 60.0
    inactivity_hours: float = 3.0


@dataclass
class CycleBudget:
    """Chimera triple cost-cap: per-cycle / rolling-60m / per-task."""

    max_per_cycle: int = 50
    max_rolling_60m: int = 200
    max_per_task: int = 100


def cycle_due(last_run: float, interval_hours: float, *, now: float | None = None) -> bool:
    """Return True если прошло >= interval_hours с последнего запуска (last_run<=0 — никогда)."""
    if last_run <= 0:
        return True
    ts = time.time() if now is None else now
    return ts - last_run >= interval_hours * 3600.0


def check_budget(
    used_this_cycle: int,
    rolling_60m: list[float],
    per_task: int,
    *,
    budget: CycleBudget | None = None,
    now: float | None = None,
) -> str:
    """Вердикт cost-cap: 'block' (жёсткий кап) | 'throttle' (мягко) | 'allow'.

    rolling_60m — timestamp'ы вызовов; за окном (60м) не считаются.
    """
    b = budget or CycleBudget()
    ts = time.time() if now is None else now
    if used_this_cycle >= b.max_per_cycle or per_task >= b.max_per_task:
        return "block"
    recent = [t for t in rolling_60m if ts - t <= ROLLING_WINDOW_S]
    if len(recent) >= b.max_rolling_60m or used_this_cycle >= b.max_per_cycle * THROTTLE_FRACTION:
        return "throttle"
    return "allow"


class RollingCounter:
    """In-memory deque of call timestamps, pruned to the rolling window."""

    def __init__(self, window_s: float = ROLLING_WINDOW_S, maxlen: int | None = None) -> None:
        self.window_s = window_s
        self._ts: deque[float] = deque(maxlen=maxlen)

    def record(self, ts: float | None = None) -> float:
        ts = time.time() if ts is None else ts
        self._ts.append(ts)
        return ts

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - self.window_s
        while self._ts and self._ts[0] < cutoff:
            self._ts.popleft()

    def count(self, now: float | None = None) -> int:
        self.prune(now)
        return len(self._ts)

    def as_list(self) -> list[float]:
        return list(self._ts)


def nightly_gate(state_path: str | Path, *, now: float | None = None, budget: CycleBudget | None = None) -> dict[str, Any]:
    """Cycles-daemon gate для ночного прохода (S13): cycle_due + check_budget.

    Персистентный last_run (state JSON) — рестарт cron не пере-запускает
    nightly раньше интервала dream_hours. Возвращает {'action': 'run'|'skip',
    'budget': 'allow'|'throttle'|'block', 'last_run': ts}. Вызывающий после
    успешного прохода обязан записать новый last_run (record_nightly_done).
    """
    import contextlib

    p = Path(state_path)
    state: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        state = json.loads(p.read_text(encoding="utf-8"))
    last = float(state.get("last_nightly", 0) or 0)
    ts = now if now is not None else time.time()
    due = cycle_due(last, CycleConfig().dream_hours, now=ts)
    if not due:
        return {"action": "skip", "budget": "allow", "last_run": last, "reason": "cycle_not_due"}
    rolling = [float(t) for t in state.get("calls", [])]
    verdict = check_budget(0, rolling, 0, budget=budget, now=ts)
    if verdict == "block":
        return {"action": "skip", "budget": "block", "last_run": last, "reason": "budget_block"}
    return {"action": "run", "budget": verdict, "last_run": last}


def record_nightly_done(state_path: str | Path, *, now: float | None = None) -> None:
    """Записать успешное завершение nightly (last_run + rolling-вызов)."""
    import contextlib

    p = Path(state_path)
    state: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        state = json.loads(p.read_text(encoding="utf-8"))
    ts = time.time() if now is None else now
    calls = [float(t) for t in state.get("calls", []) if ts - float(t) <= ROLLING_WINDOW_S]
    calls.append(ts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"last_nightly": ts, "calls": calls}), encoding="utf-8")
