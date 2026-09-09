"""S19.2 textcat pilot: binary clause-stability classifier (stable vs ephemeral).

A two-class TextCategorizer model (scripts/train_textcat.py, self-labels from
live core_memory) resolves the route of a clause the keyword map did NOT
recognize: stable statements (decay <= 0.005) must not die in L3 episodes.

Invariant (spacy-integration.md): the model is NEVER the sole decision source
— keyword matches are untouched, below the confidence threshold → None (the
status quo), a load failure → breaker + None. Pilot: `rag.textcat` default OFF.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from shared.connection import connection_manager
from shared.circuit_breaker import breaker_registry

logger = logging.getLogger(__name__)

_nlp: Any | None = None
_resolved: bool = False  # False = not tried yet; True = the singleton resolving is done

BREAKER_NAME = "textcat_model"


def _config_get(section: str, key: str, default: Any) -> Any:
    from config import config

    return config.get(section, key, default=default)


def _model() -> Any | None:
    """Lazy-load the model; None = disabled / no artifact / broken (breaker)."""
    global _nlp, _resolved
    if _resolved:
        return _nlp
    _resolved = True
    if not bool(_config_get("rag", "textcat", default=False)):
        return None
    breaker = breaker_registry.get(BREAKER_NAME, threshold=3, recovery_timeout=60.0)
    if not breaker.allow_request():
        logger.debug("textcat breaker open — модель недоступна, fallback keyword")
        return None
    base_dir = Path(connection_manager.base_dir)
    path = Path(str(_config_get("rag", "textcat_model_path", default="") or (base_dir / "textcat_model")))
    if not path.is_dir():
        logger.debug("textcat model absent at %s — нормальный режим, fallback keyword", path)
        return None
    try:
        import spacy

        _nlp = spacy.load(str(path))
    except Exception as exc:
        breaker.record_failure()
        logger.warning("textcat model load failed (%s) — breaker, fallback keyword", exc)
        return None
    return _nlp


def reset_textcat() -> None:
    """Test helper: reset the singleton (the next classify re-reads config/model)."""
    global _nlp, _resolved
    _nlp = None
    _resolved = False


def classify(text: str) -> str | None:
    """'stable' | 'ephemeral' | None (disabled / below threshold / model absent)."""
    nlp = _model()
    if nlp is None:
        return None
    threshold = float(_config_get("rag", "textcat_threshold", default=0.9))
    breaker = breaker_registry.get(BREAKER_NAME, threshold=3, recovery_timeout=60.0)
    try:
        doc = nlp(nlp.make_doc(text[:2000]))
        cats = dict(doc.cats)
    except Exception as exc:
        breaker.record_failure()
        logger.debug("textcat inference failed: %s", exc)
        return None
    if not cats:
        return None
    best = max(cats, key=lambda k: float(cats[k]))
    if float(cats[best]) < threshold:
        return None
    return str(best)


def route_promote_stable(text: str) -> bool:
    """Distiller hook: promote a keyword-unmatched FACT to L4? Only 'stable'."""
    return classify(text) == "stable"
