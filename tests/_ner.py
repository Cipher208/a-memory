"""Optional spaCy NER models are not package dependencies.

`en_core_web_sm` / `ru_core_news_sm` are installed by the console script
`a-memory-ner` (see `[project.scripts]` in pyproject.toml), so a fresh venv —
which is exactly what the pre-commit `pytest-gate` builds — does not have them.
Tests that genuinely need a model must skip there instead of failing, and must
keep asserting exactly what they assert today where the model is present.
"""

from __future__ import annotations

import pytest


def ner_model_available(name: str) -> bool:
    try:
        import spacy

        spacy.load(name)
        return True
    except Exception:
        return False


def en_ner_available() -> bool:
    return ner_model_available("en_core_web_sm")


def ru_ner_available() -> bool:
    return ner_model_available("ru_core_news_sm")


REQUIRES_EN_NER = pytest.mark.skipif(not en_ner_available(), reason="en_core_web_sm not installed (a-memory-ner not run)")
REQUIRES_RU_NER = pytest.mark.skipif(not ru_ner_available(), reason="ru_core_news_sm not installed (a-memory-ner not run)")
