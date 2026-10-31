"""S19 ru-NER на privacy-гейте — ru_core_news_sm для кириллицы (спека S19 п.1).

Схема: кириллический ввод → ru-NER (PER/ORG/LOC, garbage-guard ≥2 симв., ≤4
токенов, не чисто-цифровые), латиница → en-NER как было. Отказ модели →
circuit breaker → en-путь/словарь/regex. Флаг rag.ru_ner (default on).
"""

from __future__ import annotations

from typing import Any

import pytest

from config import config
from mcp_server.utils.privacy import sanitize
from shared.circuit_breaker import CircuitBreaker
from tests._ner import REQUIRES_EN_NER, REQUIRES_RU_NER

RU_NER = REQUIRES_RU_NER


@RU_NER
def test_ru_ner_masks_unfamiliar_cyrillic_person(monkeypatch: pytest.MonkeyPatch) -> None:
    # Имя НЕ из словаря ru_personas — раньше en-NER его не ловил (кириллица
    # не парсится), теперь ловит ru-NER.
    monkeypatch.setattr(config, "_data", {"rag": {"ru_personas": ["мамочка"]}}, raising=False)
    out, mapping = sanitize("Аркадий Укупник позвонил насчёт контракта в последнюю пятницу.")
    assert "Укупник" not in out, f"ru-NER должен маскировать незнакомую персону: {out}"
    assert "⟨PER_" in out
    assert any("Укупник" in v for v in mapping.values()), "reverse map хранит исходник"


@RU_NER
def test_ru_ner_dictionary_tier_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # Словарная персона уходит раньше (PERSON_RU, дешёвый тир), ru-NER не нужен.
    monkeypatch.setattr(config, "_data", {"rag": {"ru_personas": ["мамочка"]}}, raising=False)
    out, _mapping = sanitize("мамочка сказала не забывать про бэкапы")
    assert "мамочка" not in out
    assert "⟨PERSON_RU_1⟩" in out


@RU_NER
def test_ru_ner_garbage_guard_short_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    """S19-спека: ru-NER шумит на коротких текстах — guard не маскирует мусор."""
    monkeypatch.setattr(config, "_data", {}, raising=False)
    for noisy in ("ок", "да", "-", "12"):
        out, _ = sanitize(noisy)
        assert "⟨PER" not in out and "⟨ORG" not in out, f"шум {noisy!r} замаскирован: {out}"


@RU_NER
def test_ru_ner_latin_path_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_data", {}, raising=False)
    out, _mapping = sanitize("John Smith met Bob at Acme Corp office")
    assert "John" not in out and "Bob" not in out, "en-NER путь не сломан"
    assert "⟨PERSON_" in out


@RU_NER
def test_ru_ner_disabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_data", {"rag": {"ru_ner": False}}, raising=False)
    # Незнакомая персона остаётся: ru-NER выключен, словарь пуст, en-NER кириллицу не берёт.
    out, _ = sanitize("Аркадий Укупник позвонил насчёт контракта.")
    assert "Укупник" in out, "флаг off: ru-NER не маскирует"


@REQUIRES_EN_NER
def test_ru_ner_breaker_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """Модель сломана → breaker деградирует к en-пути, sanitize не падает."""
    import mcp_server.utils.privacy as priv

    monkeypatch.setattr(config, "_data", {}, raising=False)
    monkeypatch.setattr(priv, "_ru_nlp", None)
    monkeypatch.setattr(priv, "_ner_breaker", None)

    class _Broken:
        def allow_request(self) -> bool:
            return True

        def record_success(self) -> None:
            pass

        def record_failure(self) -> None:
            pass

    def _boom():
        raise RuntimeError("model missing")

    monkeypatch.setattr(priv, "_ner_breaker", _Broken())
    monkeypatch.setattr("spacy.load", lambda name: (_ for _ in ()).throw(RuntimeError("no model")))
    # латиница всё равно маскируется en-путём — полный degrade жив
    out, _ = sanitize("John Smith met Bob")
    assert "John" not in out
    # ru-функция вернула None после сбоя (не выкинула)
    assert priv._get_ru_nlp() is None


# --- absent model = degradation, failing model = breakage ---------------------
# `_get_ru_nlp` docstring promises: "Model not installed → None without failures
# (degradation is a normal mode, not a breakage)". Recording absence as a
# failure made three cyrillic sanitize() calls open the global `ru_ner_model`
# breaker on any host without ru_core_news_sm — diagnose then reported degraded
# permanently. These tests pin both halves of that contract.

RU_CYRILLIC = "Аркадий Укупник позвонил насчёт контракта в пятницу, почта arkady@example.com."


def _raising(exc: BaseException) -> Any:
    def _loader(name: str) -> None:
        raise exc

    return _loader


@pytest.mark.parametrize(
    "loader",
    [
        pytest.param(
            _raising(ModuleNotFoundError("No module named 'ru_core_news_sm'")),
            id="module-missing",
        ),
        pytest.param(
            _raising(
                OSError("[E050] Can't find model 'ru_core_news_sm'. It doesn't seem to be a Python package or a valid path to a data directory.")
            ),
            id="spacy-e050",
        ),
    ],
)
def test_ru_ner_absent_model_does_not_trip_breaker(monkeypatch: pytest.MonkeyPatch, loader: Any) -> None:
    """Missing optional model is a normal mode: no breaker failure, no degrade."""
    import mcp_server.utils.privacy as priv

    breaker = CircuitBreaker(threshold=3, recovery_timeout=60.0, name="ru_ner_absent_probe")
    monkeypatch.setattr(config, "_data", {}, raising=False)
    monkeypatch.setattr(priv, "_ru_nlp", None)
    monkeypatch.setattr(priv, "_ner_breaker", breaker)
    monkeypatch.setattr("spacy.load", loader)

    for _ in range(4):
        out, _mapping = sanitize(RU_CYRILLIC, use_ner=True)
        assert "⟨EMAIL_1⟩" in out  # the regex tier still ran without NER

    assert breaker.failures == 0, "absence must not be recorded as a breakage"
    assert breaker.state.value == "closed"


def test_ru_ner_runtime_error_still_trips_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model present but failing at inference IS a breakage — breaker must see it."""
    import mcp_server.utils.privacy as priv

    class _Exploding:
        def __call__(self, text: str) -> None:
            raise RuntimeError("inference blew up")

    breaker = CircuitBreaker(threshold=3, recovery_timeout=60.0, name="ru_ner_runtime_probe")
    monkeypatch.setattr(config, "_data", {}, raising=False)
    monkeypatch.setattr(priv, "_ru_nlp", _Exploding())
    monkeypatch.setattr(priv, "_ner_breaker", breaker)

    out, _mapping = sanitize(RU_CYRILLIC, use_ner=True)
    assert "⟨EMAIL_1⟩" in out
    assert breaker.failures == 1, "a loaded model failing at inference is a real failure"


def test_ru_ner_load_runtime_error_still_trips_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Installed but broken model (load raises RuntimeError) is a breakage too."""
    import mcp_server.utils.privacy as priv

    breaker = CircuitBreaker(threshold=3, recovery_timeout=60.0, name="ru_ner_load_probe")
    monkeypatch.setattr(config, "_data", {}, raising=False)
    monkeypatch.setattr(priv, "_ru_nlp", None)
    monkeypatch.setattr(priv, "_ner_breaker", breaker)
    monkeypatch.setattr("spacy.load", _raising(RuntimeError("model is corrupt")))

    assert priv._get_ru_nlp() is None
    assert breaker.failures == 1
