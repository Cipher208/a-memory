"""Task C5: eval-метрики расширение — NDCG@k, drift, negative-control, two-judge.

S11: NDCG (ранжирование), drift (устаревшее знание в KU-паре), negative-control
(протокол информативности метрики), two-judge (proxy + strict intersection).
"""

from __future__ import annotations

import math

from eval.datasets import MINI_DATASET
from eval.harness import EvalReport, is_drift, ndcg_at_k, run_eval, strict_judge
from eval.report import render_markdown, render_negative_control


def _report(arm: str, acc: float) -> EvalReport:
    return EvalReport(
        arm=arm,
        dataset="mini",
        accuracy=acc,
        recall_at5=None,
        precision=acc,
        noise_isolation=1.0 - acc,
        reacquisition_calls=0,
        construction_tokens=0,
    )


# --- NDCG@k ---


def test_ndcg_perfect_ranking_is_one() -> None:
    """Идеальное ранжирование (релевантные сверху) → NDCG = 1.0."""
    assert ndcg_at_k([1.0, 1.0, 1.0]) == 1.0
    assert ndcg_at_k([1.0, 0.0, 0.0]) == 1.0  # единственный релевантный на позиции 1
    assert ndcg_at_k([1.0, 1.0, 0.0, 0.0]) == 1.0  # релевантные в топе — идеальный порядок


def test_ndcg_known_value() -> None:
    """Классическая формула: DCG = rel_i / log2(i+1), 1-based."""
    assert math.isclose(ndcg_at_k([0.0, 1.0, 0.0]), 1.0 / math.log2(3))
    assert math.isclose(ndcg_at_k([1.0, 1.0, 0.0]), 1.0)  # релевантные на топ-2 → DCG == IDCG


def test_ndcg_zero_when_no_relevant() -> None:
    assert ndcg_at_k([0.0, 0.0, 0.0]) == 0.0
    assert ndcg_at_k([]) == 0.0


def test_ndcg_k_truncates() -> None:
    """Релевантность за пределами k не влияет; k обрезает список."""
    assert ndcg_at_k([0.0, 0.0, 1.0], k=2) == 0.0
    assert ndcg_at_k([1.0, 1.0, 0.0], k=2) == 1.0


# --- Drift (KU old/new confusion) ---


def test_drift_ku_pair_caught() -> None:
    """Старое значение в ответе, нового нет → drift=1; новое есть → не drift."""
    assert is_drift("Linear", "Jira", "Раньше для трекинга задач использовали Jira")
    assert not is_drift("Linear", "Jira", "Linear — переход с Jira")  # новое знание присутствует
    assert not is_drift("Linear", "Jira", "пользуемся Linear")
    assert not is_drift("Linear", "Jira", "борщ со свёклой")  # ни старого, ни нового
    assert not is_drift("Пётр", "", "Пётр отвечает за CI")  # нет old-expected → неприменим


async def test_run_eval_report_has_new_fields() -> None:
    """run_eval: ndcg_at5/drift_score/accuracy_strict заполнены; strict — консервативная оценка."""
    report = await run_eval("mini", "full", limit=10)
    assert 0.0 <= report.ndcg_at5 <= 1.0
    assert report.ndcg_at5 > 0.0, "релевантные хиты попадают в топ-5 хотя бы по части вопросов"
    assert 0.0 <= report.drift_score <= 1.0
    assert report.drift_score == 0.0, "система отдаёт новую сессию s_linear_new — устаревшего знания нет"
    assert 0.0 <= report.accuracy_strict <= report.accuracy + 1e-9


def test_mini_dataset_ku_pair_carries_old_answer() -> None:
    """KU-pair new-вопрос знает старое значение (для drift-детекции)."""
    q3 = next(q for q in MINI_DATASET if q.q_id == "mini-q3")
    assert q3.old_expected_answer == "Jira"
    q4 = next(q for q in MINI_DATASET if q.q_id == "mini-q4")
    assert q4.old_expected_answer == ""  # old-state вопрос: drift неприменим


# --- Two-judge ---


def test_strict_judge_exact_match() -> None:
    """Strict judge: ВСЕ токены expected должны быть в ответе (overlap == 1.0)."""
    assert strict_judge("gateway billing notifier", "Сервисы: gateway, billing и notifier")
    assert not strict_judge("gateway billing notifier", "Сервисы: gateway и billing")
    assert strict_judge("Jira", "Раньше использовали Jira")
    assert not strict_judge("Jira", "Сегодня борщ на обед")
    # abstention: оба пусты → correct; ответ на пустой expected → fail
    assert strict_judge("", "")
    assert not strict_judge("", "галактики")


def test_render_markdown_includes_new_metrics() -> None:
    md = render_markdown([_report("full", 0.8)])
    assert "ndcg@5" in md and "strict" in md and "drift" in md


# --- Negative-control protocol ---


def test_negative_control_pass_on_real() -> None:
    """Shuffled accuracy < real accuracy для каждого арма → PASS."""
    real = [_report("full", 0.9), _report("rrf", 0.8)]
    shuffled = [_report("full", 0.0), _report("rrf", 0.1)]
    md = render_negative_control(real, shuffled)
    assert "| PASS |" in md
    assert md.count("| FAIL |") == 0
    assert "| full |" in md and "| rrf |" in md


def test_negative_control_fail_on_noninformative_metric() -> None:
    """Shuffled >= real → протокол провален (метрика не чувствительна к паре вопрос↔ответ)."""
    md = render_negative_control([_report("full", 0.5)], [_report("full", 0.5)])
    assert md.count("| FAIL |") == 1
    md2 = render_negative_control([_report("full", 0.4)], [_report("full", 0.9)])
    assert md2.count("| FAIL |") == 1


def test_negative_control_missing_arm_fails() -> None:
    """Арм без shuffled-пары не может быть верифицирован → FAIL."""
    md = render_negative_control([_report("full", 0.9)], [])
    assert md.count("| FAIL |") == 1


async def test_negative_control_protocol_on_mini() -> None:
    """E2E протокол: shuffled-expected прогон даёт accuracy < real → PASS на mini."""
    from eval.harness import run_eval

    real = await run_eval("mini", "full", limit=10)
    shuf = await run_eval("mini", "full", limit=10, shuffle_expected=True)
    assert shuf.accuracy < real.accuracy, f"shuffled {shuf.accuracy} >= real {real.accuracy}"
    assert shuf.config["shuffle_expected"] is True
    assert real.config["shuffle_expected"] is False
    md = render_negative_control([real], [shuf])
    assert "| PASS |" in md
