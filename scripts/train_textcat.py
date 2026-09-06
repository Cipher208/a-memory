"""S19.2 textcat pilot: train a 2-class clause classifier (stable vs ephemeral).

Stable kinds (decay ≤ 0.005) → L4 invariants; ephemeral → events. Data:
live core_memory self-labels (shared/textcat_data.collect_rows, all instances).
Integration contract: model ONLY resolves the keyword-map-missed FACT bucket's
route (l3 → l4) — keyword matches keep their specific kinds (higher precision).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.textcat_data import class_balance, collect_rows

MODEL_LABELS = ("stable", "ephemeral")


def _split(rows: list[tuple[str, str]], dev_frac: float = 0.2, seed: int = 42) -> tuple[list, list]:
    rng = random.Random(seed)
    shuffled = sorted(rows, key=lambda r: (r[1], r[0]))  # stratify by label
    rng.shuffle(shuffled)
    n_dev = max(1, int(len(shuffled) * dev_frac))
    return shuffled[n_dev:], shuffled[:n_dev]


def _examples(nlp, rows):  # type: ignore[no-untyped-def]
    from spacy.training import Example

    out = []
    for text, label in rows:
        doc = nlp.make_doc(text)
        cats = {lbl: (1.0 if lbl == label else 0.0) for lbl in MODEL_LABELS}
        out.append(Example.from_dict(doc, {"cats": cats}))
    return out


def train(out_dir: str, epochs: int = 30, seed: int = 42, dev_frac: float = 0.2) -> dict:
    import spacy

    rows = collect_rows()
    if not rows:
        raise SystemExit("no training data collected")
    train_rows, dev_rows = _split(rows, dev_frac=dev_frac, seed=seed)
    balance = class_balance(train_rows)
    # oversample minority to ~balance (stable is the minority ~5x)
    ratio = max(1, balance["ephemeral"] // max(1, balance["stable"]))
    train_rows = train_rows + [r for r in train_rows if r[1] == "stable"] * (ratio - 1)

    nlp = spacy.load("ru_core_news_sm")  # frozen tok2vec + ru morphology
    for pipe in list(nlp.pipe_names):
        if pipe != "textcat":
            nlp.disable_pipes(pipe)
    textcat = nlp.add_pipe("textcat", last=True)
    for lbl in MODEL_LABELS:
        textcat.add_label(lbl)

    train_examples = _examples(nlp, train_rows)
    optimizer = nlp.initialize(lambda: train_examples)
    t0 = time.time()
    for epoch in range(epochs):
        losses: dict[str, float] = {}
        rng = random.Random(seed + epoch)
        rng.shuffle(train_examples)
        for batch in spacy.util.minibatch(train_examples, size=16):
            nlp.update(batch, sgd=optimizer, losses=losses, drop=0.2)
        if epoch % 10 == 0 or epoch == epochs - 1:
            print(f"epoch {epoch}: loss={losses.get('textcat', 0):.3f}", flush=True)
    train_seconds = round(time.time() - t0, 1)

    # dev eval + threshold sweep (target: stable precision high — mislabeling
    # a fact as stable pollutes L4; recall second)
    sweep: list[dict] = []
    best_threshold, best_score = 0.99, -1.0
    for threshold in [round(0.5 + 0.05 * i, 2) for i in range(10)]:
        tp = fp = fn = tn = 0
        for text, label in dev_rows:
            cats = nlp(nlp.make_doc(text)).cats
            pred = "stable" if cats["stable"] >= threshold else "ephemeral"
            if label == "stable" and pred == "stable":
                tp += 1
            elif label == "ephemeral" and pred == "stable":
                fp += 1
            elif label == "stable":
                fn += 1
            else:
                tn += 1
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        sweep.append({"threshold": threshold, "stable_precision": round(precision, 3), "stable_recall": round(recall, 3), "f1": round(f1, 3)})
        # pick: precision first (0.9 floor), then best f1
        if precision >= 0.9 and f1 > best_score:
            best_score, best_threshold = f1, threshold
    if best_score < 0:
        best_threshold = max(sweep, key=lambda s: s["f1"])["threshold"]

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    nlp.to_disk(out_path)
    metrics = {
        "rows_total": len(rows),
        "train": class_balance(train_rows),
        "dev": class_balance(dev_rows),
        "dev_size": len(dev_rows),
        "epochs": epochs,
        "train_seconds": train_seconds,
        "chosen_threshold": best_threshold,
        "sweep": sweep,
        "model_path": str(out_path),
    }
    (out_path / "train_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1))
    print(json.dumps({k: v for k, v in metrics.items() if k != "sweep"}, ensure_ascii=False, indent=1))
    print("best sweep row:", max(sweep, key=lambda s: s["f1"]))
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="model output dir (e.g. <data_dir>/textcat_model)")
    parser.add_argument("--epochs", type=int, default=30)
    args = parser.parse_args()
    train(args.out, epochs=args.epochs)
