"""Phase H Task 1: eval harness (№11) — прогон вопросов через route_query по arm абляции.

Метрики S11: accuracy (proxy-judge fuzzy match), recall@5 (evidence-пересечение),
precision (relevant_hits / all_hits), noise_isolation (1 − precision),
reacquisition_calls (retrieval-вызовы сверх одного на вопрос — D-Mem escalation),
construction_tokens (длина сконструированного контекста, прокси ~4 симв/токен).
Task C5: ndcg_at5 (качество ранжирования), accuracy_strict (two-judge intersection),
drift_score (устаревшее знание в KU-паре old/new), negative-control протокол
(shuffle_expected=True + render_negative_control в eval/report.py).
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import random
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+")

# JudgeFn: async (question, expected_answer, answer) -> correct?
JudgeFn = Callable[[str, str, str], Awaitable[bool]]


def _toks(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 3}


def _match(a: str, b: str) -> bool:
    """Token-match с учётом RU-морфологии: равенство или общий префикс ≥ 5 («вторник»~«вторникам»)."""
    return a == b or (len(a) >= 5 and len(b) >= 5 and (a.startswith(b) or b.startswith(a) or a[:5] == b[:5]))


def _overlap(exp: set[str], got: set[str]) -> float:
    """Доля ожидаемых токенов, найденных в ответе (prefix-aware)."""
    if not exp:
        return 0.0
    return sum(1 for e in exp if any(_match(e, g) for g in got)) / len(exp)


@dataclass
class EvalReport:
    arm: str
    dataset: str
    accuracy: float
    recall_at5: float | None
    precision: float
    noise_isolation: float
    reacquisition_calls: int
    construction_tokens: int
    ndcg_at5: float = 0.0
    accuracy_strict: float = 0.0
    drift_score: float = 0.0
    per_category: dict[str, float] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)


# Proxy-judge: token-overlap ≥ 0.5 → correct (smoke). LLM-judge — интерфейс-заглушка judge_fn.
def proxy_judge(expected_answer: str, answer: str) -> bool:
    """Fuzzy match: доля ожидаемых токенов в ответе ≥ 0.5; пустой expected (abstention) → ответ должен быть пуст."""
    exp = _toks(expected_answer)
    if not exp:
        return not _toks(answer)
    return _overlap(exp, _toks(answer)) >= 0.5


def strict_judge(expected_answer: str, answer: str) -> bool:
    """Exact-match strict judge: ВСЕ ожидаемые токены присутствуют (overlap == 1.0); abstention — пустой ответ.

    Пересечение с proxy-judge = консервативная оценка accuracy (accuracy_strict).
    """
    exp = _toks(expected_answer)
    if not exp:
        return not _toks(answer)
    return _overlap(exp, _toks(answer)) >= 1.0


def ndcg_at_k(ranked_relevance: list[float], k: int = 5) -> float:
    """NDCG@k: DCG = Σ rel_i / log2(i+1) по топ-k (i 1-based); IDCG — DCG отсортированных по убыванию.

    Идеальный порядок → 1.0; IDCG = 0 (нет релевантных) → 0.0.
    """

    def dcg(rels: list[float]) -> float:
        return sum(rel / math.log2(i + 1) for i, rel in enumerate(rels[:k], start=1))

    idcg = dcg(sorted(ranked_relevance, reverse=True))
    if idcg <= 0.0:
        return 0.0
    return dcg(ranked_relevance) / idcg


def is_drift(expected_answer: str, old_expected_answer: str, answer: str) -> bool:
    """KU-drift: ответ содержит СТАРОЕ значение (token-overlap ≥ 0.5), но не новое — устаревшее знание.

    Пустой old_expected (old-state вопрос или не KU) → drift неприменим.
    """
    if not old_expected_answer:
        return False
    ans = _toks(answer)
    stale = _overlap(_toks(old_expected_answer), ans) >= 0.5
    fresh = _overlap(_toks(expected_answer), ans) >= 0.5
    return stale and not fresh


async def _proxy_judge_async(question: str, expected: str, answer: str) -> bool:
    return proxy_judge(expected, answer)


async def _strict_judge_async(question: str, expected: str, answer: str) -> bool:
    return strict_judge(expected, answer)


class _CountingRAG:
    """Прокси над RAGEngine: считает retrieval-вызовы (escalations в route_query) — метрика reacquisition."""

    def __init__(self, engine: Any):
        self._engine = engine
        self.search_calls = 0

    async def search(self, query: str, user_id: str = "default", strategy: str = "hybrid", limit: int | None = None) -> list[dict[str, Any]]:
        self.search_calls += 1
        out: list[dict[str, Any]] = await self._engine.search(query, user_id=user_id, strategy=strategy, limit=limit)
        return out


async def run_eval(
    dataset: str,
    arm: str,
    *,
    limit: int = 50,
    judge_fn: JudgeFn | None = None,
    shuffle_expected: bool = False,
    shuffle_seed: int = 42,
) -> EvalReport:
    """Прогон датасета через route_query при RETRIEVAL_MODE=arm.

    Env RETRIEVAL_MODE ставится только на время прогона и восстанавливается.
    judge_fn — интерфейс LLM-judge; None → proxy fuzzy-match. Память —
    изолированный tmp-инстанс: evidence-сессии инжестятся в rag-корпус
    (RAGEngine) и L4 core — реалистичная multi-source выдача.

    shuffle_expected=True — negative-control: expected-ответы перемешиваются
    (детерминированно, shuffle_seed), пара вопрос↔ответ рвётся. Рабочий
    judge обязан просесть (accuracy < real), иначе метрика неинформативна
    (протокол: eval/report.py::render_negative_control).
    """
    from rag.ablation import RETRIEVAL_MODES

    if arm not in RETRIEVAL_MODES:
        raise ValueError(f"unknown arm {arm!r}; expected one of {RETRIEVAL_MODES}")

    from core.memory import CoreMemory
    from eval.datasets import load_eval_bundle
    from rag.engine import RAGEngine
    from rag.multi_source import MultiSourceRAG
    from shared.connection import connection_manager
    from shared.migrations import MigrationManager

    questions, sessions = await load_eval_bundle(dataset, limit)
    if shuffle_expected and len(questions) > 1:
        rng = random.Random(shuffle_seed)
        expected = [q.expected_answer for q in questions]
        rng.shuffle(expected)
        questions = [replace(q, expected_answer=exp) for q, exp in zip(questions, expected, strict=True)]

    prev_mode = os.environ.get("RETRIEVAL_MODE")
    os.environ["RETRIEVAL_MODE"] = arm
    original_dir = connection_manager.base_dir
    connection_manager.base_dir = Path(tempfile.mkdtemp(prefix="ariel-eval-"))
    connection_manager._conns.clear()
    try:
        await MigrationManager(cm=connection_manager).migrate()
        engine = RAGEngine(cm=connection_manager)
        await engine.init_db()
        core = CoreMemory(cm=connection_manager, layer="user")
        await core._init_db()
        for sid, text in sessions.items():
            await engine.ingest_text(title=sid, text=text, user_id="eval", wiki_type="session")
            await core.save("eval", sid, text, importance=0.8, source="eval")

        rag = _CountingRAG(engine)
        multi = MultiSourceRAG(rag=rag, wiki=None, cm=connection_manager)
        judge: JudgeFn = judge_fn if judge_fn is not None else _proxy_judge_async
        return await _score(
            questions,
            multi,
            rag,
            arm,
            judge,
            dataset,
            judge_name="proxy" if judge_fn is None else "custom",
            shuffle_expected=shuffle_expected,
        )
    finally:
        if prev_mode is None:
            os.environ.pop("RETRIEVAL_MODE", None)
        else:
            os.environ["RETRIEVAL_MODE"] = prev_mode
        with contextlib.suppress(Exception):
            await connection_manager.close_all()
        connection_manager._conns.clear()
        connection_manager.base_dir = original_dir


async def _score(
    questions: list[Any],
    multi: Any,
    counting_rag: _CountingRAG,
    arm: str,
    judge: JudgeFn,
    dataset: str,
    *,
    judge_name: str,
    shuffle_expected: bool = False,
) -> EvalReport:
    from rag.dual_route import route_query

    correct = 0
    strict_correct = 0
    relevant_hits = 0
    all_hits = 0
    recall_hits = 0
    recall_total = 0
    constructed_chars = 0
    ndcg_sum = 0.0
    ndcg_n = 0
    n_ku = 0
    n_drift = 0
    per_cat_total: dict[str, int] = {}
    per_cat_correct: dict[str, int] = {}
    n_questions = 0

    for q in questions:
        n_questions += 1
        hits = await route_query(multi, q.question, user_id="eval", limit=10, cm=multi.cm)
        constructed_chars += sum(len(str(h.get("content") or "")) for h in hits)
        # Ответ на judging = релевантная часть выдачи: хиты, делящие токены с
        # вопросом. Шумные хиты с нулевым пересечением (hash-embedding шум) не
        # считаются ответом — честный ответчик по такому контексту воздержался бы.
        qtok = _toks(q.question)
        evidence = [str(h.get("content") or "") for h in hits if qtok & _toks(str(h.get("content") or ""))]
        answer = " ".join(evidence)
        exp = _toks(q.expected_answer)

        # NDCG@5: релевантность по позиции выдачи (hit=1, miss=0 по evidence-пересечению).
        # Abstention (пустой expected) не имеет золотого ранжирования — из среднего исключается.
        if exp:
            relevance = [1.0 if _overlap(exp, _toks(str(h.get("content") or ""))) >= 0.5 else 0.0 for h in hits]
            ndcg_sum += ndcg_at_k(relevance, 5)
            ndcg_n += 1

        for h in hits:
            all_hits += 1
            if exp and _overlap(exp, _toks(str(h.get("content") or ""))) >= 0.5:
                relevant_hits += 1

        # Two-judge: primary (judge) ∧ strict — консервативная оценка.
        is_correct = await judge(q.question, q.expected_answer, answer)
        if is_correct:
            correct += 1
            per_cat_correct[q.category] = per_cat_correct.get(q.category, 0) + 1
        if is_correct and await _strict_judge_async(q.question, q.expected_answer, answer):
            strict_correct += 1
        per_cat_total[q.category] = per_cat_total.get(q.category, 0) + 1

        # Drift: KU-pair new-вопрос, в ответе старое значение вместо нового.
        old = getattr(q, "old_expected_answer", "") or ""
        if old:
            n_ku += 1
            if is_drift(q.expected_answer, old, answer):
                n_drift += 1

        # recall@5: доля evidence-сессий в топ-5 (title == session_id)
        ev = set(q.evidence_session_ids)
        if ev:
            top5 = {str(h.get("title") or "") for h in hits[:5]}
            recall_hits += len(ev & top5)
            recall_total += len(ev)

    n = n_questions
    precision = relevant_hits / all_hits if all_hits else 0.0
    return EvalReport(
        arm=arm,
        dataset=dataset,
        accuracy=correct / n if n else 0.0,
        recall_at5=recall_hits / recall_total if recall_total else None,
        precision=precision,
        noise_isolation=1.0 - precision,
        reacquisition_calls=counting_rag.search_calls - n,
        construction_tokens=constructed_chars // 4,
        ndcg_at5=ndcg_sum / ndcg_n if ndcg_n else 0.0,
        accuracy_strict=strict_correct / n if n else 0.0,
        drift_score=n_drift / n_ku if n_ku else 0.0,
        per_category={k: per_cat_correct.get(k, 0) / v for k, v in per_cat_total.items()},
        config={"mode": arm, "judge": judge_name, "n_questions": n, "shuffle_expected": shuffle_expected},
    )
