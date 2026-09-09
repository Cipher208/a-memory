"""Code policy: hot files must have English-only comments and docstrings.

RU string literals (classification keywords, synonym maps, morphology
patterns, regex token sets) are product data and stay — only comments and
docstrings are regulated by the "comments in English" rule.
"""

import ast
import io
import tokenize
from pathlib import Path

import pytest

HOT_FILES = [
    "lifecycle/graph_miners.py",
    "rag/edm.py",
    "lifecycle/distiller.py",
    "rag/dual_route.py",
    "rag/ablation.py",
]

ROOT = Path(__file__).resolve().parents[1]


def _has_cyrillic(text: str) -> bool:
    return any("\u0400" <= ch <= "\u04ff" for ch in text)


@pytest.mark.parametrize("relpath", HOT_FILES)
def test_comments_are_english(relpath: str) -> None:
    src = (ROOT / relpath).read_text(encoding="utf-8")
    offenders = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT and _has_cyrillic(tok.string):
            offenders.append(f"line {tok.start[0]}: {tok.string[:70]}")
    assert not offenders, f"{relpath} RU comments:\n" + "\n".join(offenders)


@pytest.mark.parametrize("relpath", HOT_FILES)
def test_docstrings_are_english(relpath: str) -> None:
    src = (ROOT / relpath).read_text(encoding="utf-8")
    offenders = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc and _has_cyrillic(doc):
                name = getattr(node, "name", "<module>")
                first = doc.strip().splitlines()[0][:70]
                offenders.append(f"{name}: {first}")
    assert not offenders, f"{relpath} RU docstrings:\n" + "\n".join(offenders)
