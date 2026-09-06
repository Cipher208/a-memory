"""S20 прогон 3: LongMemEval-S на DENSE эмбеддингах (e5-small).

Отличие от прогона 2 (hash): env ARIEL_HASH_EMBEDDINGS снят; fail-fast — если
_get_model() возвращает None (модель не скачалась/пакет отсутствует), прогон
abort'ится с ошибкой, а не молча деградирует в hash (тот silent-fallback, ради
которого сравнение было бы недействительным).

Arms: full (победитель двух прогонов) + full_shuffled (negative control).
Запуск из корня: .venv/bin/python scripts/run_lme_dense.py
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.pop("ARIEL_HASH_EMBEDDINGS", None)  # fail-fast ниже проверит реальность
os.environ["BACKUP_CRON_DISABLED"] = "1"
os.environ["MCP_MASTER_KEY"] = "lme-eval-key"

from eval.harness import run_eval

RESULTS_PATH = Path("/tmp/lme_dense_results.json")
LIMIT = 50

RUNS: tuple[tuple[str, str, bool], ...] = (
    ("full_dense", "full", False),
    ("full_dense_shuffled", "full", True),
)


def _fail_fast_dense() -> None:
    """Dense-модель обязана резолвиться, иначе сравнение бессмысленно."""
    from shared.embeddings import _get_model

    if _get_model() is None:
        print("FATAL: dense model not available (_get_model() -> None); check sentence-transformers install and network", file=sys.stderr)
        raise SystemExit(2)


def _load_results() -> dict[str, Any]:
    return json.loads(RESULTS_PATH.read_text()) if RESULTS_PATH.is_file() else {}


def _save_results(existing: dict[str, Any]) -> None:
    RESULTS_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=1))


async def main() -> None:
    _fail_fast_dense()
    existing = _load_results()
    for name, arm, shuffle in RUNS:
        if name in existing:
            print(f"skip {name} (done)", flush=True)
            continue
        t0 = time.time()
        report = await run_eval("longmemeval_s", arm, limit=LIMIT, shuffle_expected=shuffle)
        dt = time.time() - t0
        row = {k: v for k, v in report.__dict__.items() if not isinstance(v, dict)}
        row["_seconds"] = round(dt)
        existing[name] = row
        _save_results(existing)
        print(
            f"DONE {name} in {dt:.0f}s: acc={row['accuracy']:.3f} strict={row['accuracy_strict']:.3f} recall@5={row['recall_at5']} tok={row['construction_tokens']}",
            flush=True,
        )
        # aiosqlite worker-thread гонка: один run_eval на процесс
        os._exit(0)
    print("ALL DENSE RUNS COMPLETE", flush=True)


asyncio.run(main())
os._exit(0)
