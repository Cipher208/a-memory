"""Phase H Task 1: eval report — Markdown-таблица arms×метрики + честная плашка конфига.

Task C5: render_negative_control — протокол информативности метрики. Shuffled-arms
(expected-ответы перемешаны, пара вопрос↔ответ разорвана) ДОЛЖНЫ дать accuracy
строго меньше real-arms: рабочий judge чувствителен к паре. Shuffled ≥ real →
FAIL (метрика неинформативна — не отличает правильные ответы от шума).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eval.harness import EvalReport


def render_markdown(reports: list[EvalReport]) -> str:
    """Таблица arms×метрики + плашка конфига (judge, размер датасета)."""
    lines = [
        "# Eval report — ablation arms",
        "",
        "| arm | accuracy | strict | ndcg@5 | drift | recall@5 | precision | noise_isolation | reacquisition | construction_tokens |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in reports:
        recall = "n/a" if r.recall_at5 is None else f"{r.recall_at5:.3f}"
        lines.append(
            f"| {r.arm} | {r.accuracy:.3f} | {r.accuracy_strict:.3f} | {r.ndcg_at5:.3f} | {r.drift_score:.3f} | {recall} | {r.precision:.3f} | {r.noise_isolation:.3f} | {r.reacquisition_calls} | {r.construction_tokens} |"
        )
    lines.append("")
    for r in reports:
        cats = ", ".join(f"{k}={v:.2f}" for k, v in sorted(r.per_category.items())) or "-"
        lines.append(f"**{r.arm}** ({r.dataset}, n={r.config.get('n_questions', '?')}): per-category: {cats}; config: `{r.config}`")
    return "\n".join(lines) + "\n"


def render_negative_control(reports_real: list[EvalReport], reports_shuffled: list[EvalReport]) -> str:
    """Negative-control протокол: per-arm PASS/FAIL вердикт.

    shuffled-arms ДОЛЖНЫ дать accuracy < real того же арма (judge падает на
    разорванной паре вопрос↔ответ). Арм без shuffled-пары верифицировать
    нельзя → FAIL (консервативно).
    """
    shuffled_by_arm = {r.arm: r for r in reports_shuffled}
    lines = [
        "# Negative-control protocol",
        "",
        "Критерий: shuffled-accuracy < real-accuracy (judge чувствителен к паре вопрос↔ответ).",
        "",
        "| arm | accuracy | shuffled | verdict |",
        "|---|---|---|---|",
    ]
    for r in reports_real:
        s = shuffled_by_arm.get(r.arm)
        if s is None:
            verdict, note = "FAIL", "нет shuffled-пары"
        elif s.accuracy < r.accuracy:
            verdict, note = "PASS", f"shuffled {s.accuracy:.3f} < real {r.accuracy:.3f}"
        else:
            verdict, note = "FAIL", f"shuffled {s.accuracy:.3f} >= real {r.accuracy:.3f} — метрика неинформативна"
        lines.append(f"| {r.arm} | {r.accuracy:.3f} | {'n/a' if s is None else f'{s.accuracy:.3f}'} | {verdict} | {note} |")
    lines.append("")
    return "\n".join(lines) + "\n"
