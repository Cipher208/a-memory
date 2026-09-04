"""Phase H Task 1: eval report — Markdown-таблица arms×метрики + честная плашка конфига."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eval.harness import EvalReport


def render_markdown(reports: list[EvalReport]) -> str:
    """Таблица arms×метрики + плашка конфига (judge, размер датасета)."""
    lines = [
        "# Eval report — ablation arms",
        "",
        "| arm | accuracy | recall@5 | precision | noise_isolation | reacquisition | construction_tokens |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in reports:
        recall = "n/a" if r.recall_at5 is None else f"{r.recall_at5:.3f}"
        lines.append(
            f"| {r.arm} | {r.accuracy:.3f} | {recall} | {r.precision:.3f} | {r.noise_isolation:.3f} | {r.reacquisition_calls} | {r.construction_tokens} |"
        )
    lines.append("")
    for r in reports:
        cats = ", ".join(f"{k}={v:.2f}" for k, v in sorted(r.per_category.items())) or "-"
        lines.append(f"**{r.arm}** ({r.dataset}, n={r.config.get('n_questions', '?')}): per-category: {cats}; config: `{r.config}`")
    return "\n".join(lines) + "\n"
