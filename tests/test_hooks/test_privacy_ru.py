"""C4: ru-persona tier — словарные персоны маскируются до spaCy.

en-NER не ловит sentence-initial кириллические имена («Оператор красив») —
известные персоны проекта (config rag.ru_personas + классы rag.synonyms)
маскирует структурный словарь-тир с placeholder ⟨PERSON_RU_N⟩.
"""

from __future__ import annotations

from mcp_server.utils.privacy import sanitize


def test_mamochka_masked() -> None:
    out, m = sanitize("Мамочка сказала перейти на PostgreSQL", use_ner=True)
    assert "Мамочка" not in out
    assert "⟨PERSON_RU_1⟩ сказала перейти" in out
    assert m["⟨PERSON_RU_1⟩"] == "Мамочка"


def test_lili_sentence_initial_masked() -> None:
    # sentence-initial: en-NER это пропускает, словарь — нет
    out, _m = sanitize("Оператор красив", use_ner=True)
    assert "Оператор" not in out
    assert out.startswith("⟨PERSON_RU_1⟩")


def test_lisyonysh_masked() -> None:
    out, _m = sanitize("Persona_a спит", use_ner=True)
    assert "Persona_a" not in out
    assert "⟨PERSON_RU_1⟩ спит" in out


def test_placeholder_stability_russian() -> None:
    out, m = sanitize("Оператор пришёл, потом Оператор ушёл", use_ner=True)
    assert out.count("⟨PERSON_RU_1⟩") == 2
    assert m["⟨PERSON_RU_1⟩"] == "Оператор"


def test_config_override_adds_persona(monkeypatch) -> None:
    # словарь НЕ хардкодится: новые персоны добавляются через config rag.ru_personas
    from config import config

    monkeypatch.setattr(config, "_data", {"rag": {"ru_personas": ["оператор"]}}, raising=False)
    out, _m = sanitize("Оператор прыгнул на стол", use_ner=False)
    assert "Оператор" not in out
    assert "⟨PERSON_RU_1⟩ прыгнул на стол" in out
