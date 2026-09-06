"""S19 ru-NER на privacy-гейте — ru_core_news_sm для кириллицы (спека S19 п.1).

Схема: кириллический ввод → ru-NER (PER/ORG/LOC, garbage-guard ≥2 симв., ≤4
токенов, не чисто-цифровые), латиница → en-NER как было. Отказ модели →
circuit breaker → en-путь/словарь/regex. Флаг rag.ru_ner (default on).
"""

from __future__ import annotations

import pytest

from config import config
from mcp_server.utils.privacy import sanitize


def _ru_nlp_available() -> bool:
    try:
        import spacy

        spacy.load("ru_core_news_sm")
        return True
    except Exception:
        return False


RU_NER = pytest.mark.skipif(not _ru_nlp_available(), reason="ru_core_news_sm not installed")


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
