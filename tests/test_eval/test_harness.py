"""Phase H Task 1: eval harness smoke — MINI_DATASET → run_eval → метрики/arms/abstention."""

from __future__ import annotations

import json
from pathlib import Path


from eval.datasets import MINI_DATASET, MINI_EVIDENCE, EvalQuestion, load_longmemeval_s
from eval.harness import EvalReport, proxy_judge, run_eval
from eval.report import render_markdown


def test_mini_dataset_covers_longmemeval_categories() -> None:
    """MINI_DATASET: 10 вопросов, категории LongMemEval, KU-пара old/new, abstention."""
    assert len(MINI_DATASET) == 10
    cats = {q.category for q in MINI_DATASET}
    assert {"multi_session", "knowledge_update", "temporal", "enumerative", "abstention"} <= cats
    # KU-пара old/new присутствует: один вопрос — новое значение, другой — старое
    ku = [q for q in MINI_DATASET if q.category == "knowledge_update"]
    assert len(ku) >= 2
    abstention = [q for q in MINI_DATASET if q.category == "abstention"]
    assert abstention and abstention[0].expected_answer == "", "abstention: пустой expected = должен воздержаться"
    # evidence-сессии указывают в корпус
    for q in MINI_DATASET:
        assert set(q.evidence_session_ids) <= set(MINI_EVIDENCE), f"{q.q_id}: нет сессии в корпусе"


async def test_load_longmemeval_s_fallback_mini() -> None:
    """HF недоступен (offline/no datasets lib) → fallback на MINI_DATASET."""
    questions = await load_longmemeval_s(limit=3)
    assert len(questions) == 3
    assert all(isinstance(q, EvalQuestion) for q in questions)


async def test_run_eval_full_report_filled() -> None:
    """run_eval(arm='full') → EvalReport с заполненными метриками."""
    report = await run_eval("mini", "full", limit=10)
    assert isinstance(report, EvalReport)
    assert report.arm == "full" and report.dataset == "mini"
    assert report.config["n_questions"] == 10
    for name in ("accuracy", "precision", "noise_isolation"):
        assert 0.0 <= getattr(report, name) <= 1.0, f"{name} вне [0,1]: {getattr(report, name)}"
    assert report.recall_at5 is not None and 0.0 <= report.recall_at5 <= 1.0
    assert report.reacquisition_calls >= 0
    assert report.construction_tokens > 0, "контекст конструируется из хитов"
    assert report.per_category, "per_category breakdown заполнен"
    assert set(report.per_category) == {"multi_session", "knowledge_update", "temporal", "enumerative", "abstention"}


async def test_arms_both_complete() -> None:
    """rrf vs full: результаты могут совпасть на mini — оба прошли без ошибки и различимы по arm."""
    r1 = await run_eval("mini", "rrf", limit=10)
    r2 = await run_eval("mini", "full", limit=10)
    assert r1.arm == "rrf" and r2.arm == "full"
    assert 0.0 <= r1.accuracy <= 1.0 and 0.0 <= r2.accuracy <= 1.0


async def test_abstention_false_premise() -> None:
    """False-premise вопрос: система НЕ отвечает → abstention = 1.0; ответ → fail (0.0)."""
    report = await run_eval("mini", "full", limit=10)
    assert report.per_category["abstention"] == 1.0, (
        f"система ответила на false-premise — abstention помечен fail: {report.per_category['abstention']}"
    )
    # proxy-judge: непустой ответ на abstention-вопрос = fail
    assert proxy_judge("", "Дарт Вейдер правит галактикой") is False
    assert proxy_judge("", "") is True


def test_proxy_judge_token_overlap() -> None:
    """proxy fuzzy match: overlap ≥ 0.5 = correct."""
    assert proxy_judge("gateway billing notifier", "Сервисы: gateway, billing и notifier") is True
    assert proxy_judge("Jira", "Раньше использовали Jira") is True
    assert proxy_judge("Jira", "Сегодня борщ на обед") is False
    assert proxy_judge("первую неделю января", "Квартальное планирование в первую неделю января") is True


async def test_custom_judge_fn_used() -> None:
    """LLM-judge интерфейс: judge_fn подменяет proxy, получает все вопросы."""
    calls: list[str] = []

    async def judge(question: str, expected: str, answer: str) -> bool:
        calls.append(question)
        return True

    report = await run_eval("mini", "rrf", limit=10, judge_fn=judge)
    assert report.config["judge"] == "custom"
    assert report.accuracy == 1.0
    assert len(calls) == 10


async def test_retrieval_mode_restored() -> None:
    """Env RETRIEVAL_MODE — только на время прогона (monkeypatch внутри run, не глобально)."""
    import os

    await run_eval("mini", "full", limit=10)
    assert "RETRIEVAL_MODE" not in os.environ


def test_arms_config_valid() -> None:
    """configs/arms.json: 4 арма с компонентами EDM/inhibition/graph_expand."""
    cfg = json.loads((Path(__file__).parents[2] / "eval" / "configs" / "arms.json").read_text(encoding="utf-8"))
    assert set(cfg["arms"]) == {"rrf", "dense_per_kind", "gated", "full"}
    assert cfg["default"] == "full"
    assert cfg["arms"]["full"]["edm"] is True and cfg["arms"]["rrf"]["edm"] is False


def test_render_markdown_table() -> None:
    """render_markdown: таблица arms×метрики + плашка конфига."""
    from dataclasses import replace

    report = EvalReport(
        arm="full",
        dataset="mini",
        accuracy=0.8,
        recall_at5=0.9,
        precision=0.75,
        noise_isolation=0.25,
        reacquisition_calls=2,
        construction_tokens=100,
        per_category={"multi_session": 0.75},
        config={"mode": "full", "judge": "proxy", "n_questions": 10},
    )
    other = replace(report, arm="rrf", accuracy=0.5, recall_at5=None)
    md = render_markdown([report, other])
    assert "| arm |" in md and "| full |" in md and "| rrf |" in md
    assert "n/a" in md  # recall_at5 None
    assert "proxy" in md  # честная плашка judge
    assert "0.800" in md
