"""LongMemEval-S ablation run (S20 прогон 2): 4 arms × 50 questions.

Запускается из корня репо: .venv/bin/python scripts/run_lme_s.py > log 2>&1
Результаты печатаются JSON-строкой в конце (парсится отдельно); каждый arm
дописывается в /tmp/lme_s_results.json по мере готовности (crash-safe).
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

os.environ["ARIEL_HASH_EMBEDDINGS"] = "1"
os.environ["BACKUP_CRON_DISABLED"] = "1"
os.environ["MCP_MASTER_KEY"] = "lme-eval-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.harness import run_eval

RESULTS_PATH = Path("/tmp/lme_s_results.json")
LIMIT = 50

# (имя_результата, arm, pregate, shuffle_expected)
RUNS: tuple[tuple[str, str, bool, bool], ...] = (
    ("rrf", "rrf", False, False),
    ("dense_per_kind", "dense_per_kind", False, False),
    ("gated", "gated", False, False),
    ("full", "full", False, False),
    # S17 pre-gate: прод-кандидат — full-путь (EDM/ITS жив) + урезанный fan-out.
    ("full_pregate", "full", True, False),
    # negative-control протокол: judge обязан просесть на shuffled (rrf + full).
    ("rrf_shuffled", "rrf", False, True),
    ("full_shuffled", "full", False, True),
)


def _load_results() -> dict[str, Any]:
    return json.loads(RESULTS_PATH.read_text()) if RESULTS_PATH.is_file() else {}


def _save_results(existing: dict[str, Any]) -> None:
    RESULTS_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=1))


async def main() -> None:
    existing = _load_results()
    for name, arm, pregate, shuffle in RUNS:
        if name in existing:
            print(f"skip {name} (done)", flush=True)
            continue
        if pregate:
            from config import config

            config._data = {"retrieval": {"pregate": True}}  # S17-кандидат: pre-gate ON
        t0 = time.time()
        report = await run_eval("longmemeval_s", arm, limit=LIMIT, shuffle_expected=shuffle)
        dt = time.time() - t0
        row = {k: v for k, v in report.__dict__.items() if not isinstance(v, dict)}
        row["_seconds"] = round(dt)
        existing[name] = row
        _save_results(existing)
        print(f"DONE {name} in {dt:.0f}s: acc={row['accuracy']:.3f} recall@5={row['recall_at5']} tok={row['construction_tokens']}", flush=True)
        # aiosqlite worker-thread гонка при закрытии инстанса: не даём
        # интерпретатору чистить потоки — жёсткий выход, следующий run в новом
        # процессе (bash-цикл), crash-safe results пропускает готовое.
        os._exit(0)
    print("ALL ARMS COMPLETE", flush=True)


asyncio.run(main())
os._exit(0)
