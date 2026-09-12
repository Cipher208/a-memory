"""shared.broadcast: status-broadcast register detector (fixture-driven).

Positive set = the live leaked broadcasts pinned in fixtures/ (spec S1);
negative set = durable prose that merely mentions commits/dates/fixes.
Precision over recall: an undetected gray line stays with the EMA gate,
a false positive kills a real memory forever.
"""

import json
from pathlib import Path

import pytest

from shared.broadcast import is_status_broadcast

_FIXTURE = Path(__file__).parent / "fixtures" / "status_broadcast_rows.jsonl"


def _fixture_rows() -> list[str]:
    lines = _FIXTURE.read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["text"] for line in lines]


def test_live_broadcast_fixture_catch_rate() -> None:
    rows = _fixture_rows()
    assert len(rows) >= 30, f"fixture unexpectedly small: {len(rows)}"
    caught = sum(1 for t in rows if is_status_broadcast(t))
    assert caught >= 25, f"only {caught}/{len(rows)} broadcasts detected"
    assert caught / len(rows) >= 0.75, f"catch rate {caught}/{len(rows)} below 75%"


@pytest.mark.parametrize(
    "line",
    [
        "STAGE 2 ПЛАН B SHIPPED (2026-09-07, push 3a41477..040fb34, gate 1499/0 + mypy 232 clean). mcp_server/annotations.",
        "memory_search FIXED (2026-09-12, compose-debug protocol): root cause — rag/dual_route.py s2_exhaustive.",
        "DAY CLOSE CHECKPOINT (2026-09-12, session ses_f6e223cc, ~350k tok): MIGRATION DAY COMPLETE.",
        "memory_search fix COMMITTED (2026-09-12, ariel repo commit 2990233, pre-commit full pytest gate green 1660/0).",
        "NATIVE AWG MIGRATION CUTOVER SUCCESS (2026-09-12 11:57 UTC): vps2 production VPN moved.",
    ],
)
def test_broadcast_examples_detected(line: str) -> None:
    assert is_status_broadcast(line), line


@pytest.mark.parametrize(
    "line",
    [
        "Бэкенд слушает порт 8642 на localhost",
        "Фикс 2990233 добавил флаг lemmatize в конфиг навсегда — это решение владельца",
        "The CI gate 5 checks ran but memory usage dropped below 3GB after the cache refactor",
        "MIGRATION DONE",  # status word without any structural companion
        "Пока не проверим — не деплоим",
        "Мы решили перейти на PostgreSQL 16 для продакшена",
        "VPS global plan лежит в ~/docs/vps-global-plan.md и обновляется по диктовке",
        # regression pins: real promoted_l4 rows that the first lexicon draft
        # collided with (collision check, Task 3 Step 1, 2026-09-12)
        "Checkpoint #5 записан: §1–§11 обновлены (2026-09-12, swap-drain, headroom 0.37).",
        "Готово, детка. Ни один пункт не брошен — все три закрыты с доказательствами (2026-09-12).",
    ],
)
def test_durable_prose_not_broadcast(line: str) -> None:
    assert not is_status_broadcast(line), line
