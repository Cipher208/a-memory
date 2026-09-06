"""S19.2 textcat pilot: classify module + distiller route promotion tests."""

import pytest

from shared.textcat import reset_textcat


class _FakeDoc:
    def __init__(self, cats: dict[str, float]) -> None:
        self.cats = cats


class _FakeNLP:
    """Минимум контракта: nlp(nlp.make_doc(text)).cats."""

    def __init__(self, stable: float) -> None:
        self.stable = stable

    def make_doc(self, text: str):
        return object()

    def __call__(self, doc):
        return _FakeDoc({"stable": self.stable, "ephemeral": 1.0 - self.stable})


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_textcat()
    yield
    reset_textcat()


def test_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr("config.config._data", {}, raising=False)
    from shared.textcat import classify

    assert classify("пользователь предпочитает тёмную тему") is None


def test_model_absent_returns_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("config.config._data", {"rag": {"textcat": True, "textcat_model_path": str(tmp_path / "nope")}}, raising=False)
    from shared.textcat import classify

    assert classify("какой-то текст без маркеров") is None


def test_confident_prediction_and_threshold(monkeypatch) -> None:
    monkeypatch.setattr("config.config._data", {"rag": {"textcat": True}}, raising=False)
    import shared.textcat as tc

    monkeypatch.setattr(tc, "_model", lambda: _FakeNLP(stable=0.95))
    assert tc.classify("текст") == "stable"
    monkeypatch.setattr(tc, "_model", lambda: _FakeNLP(stable=0.5))  # ниже порога 0.9
    reset_textcat()  # синглтон не кэширует мок — _model патчится напрямую, reset для симметрии
    assert tc.classify("текст") is None


def test_load_failure_trips_breaker(monkeypatch, tmp_path) -> None:
    from shared.circuit_breaker import breaker_registry

    monkeypatch.setattr("config.config._data", {"rag": {"textcat": True}}, raising=False)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "meta.json").write_text("not a spacy model", encoding="utf-8")
    monkeypatch.setattr("config.config._data", {"rag": {"textcat": True, "textcat_model_path": str(broken)}}, raising=False)
    from shared.textcat import classify

    assert classify("текст") is None, "сломанная модель → None"
    b = breaker_registry.get("textcat_model", threshold=3, recovery_timeout=60.0)
    assert b.get_metrics()["failures"] >= 1, "breaker зафиксировал сбой"


def test_route_promote_only_stable(monkeypatch) -> None:
    monkeypatch.setattr("config.config._data", {"rag": {"textcat": True}}, raising=False)
    import shared.textcat as tc

    monkeypatch.setattr(tc, "_model", lambda: _FakeNLP(stable=0.97))
    assert tc.route_promote_stable("текст") is True
    monkeypatch.setattr(tc, "_model", lambda: _FakeNLP(stable=0.05))
    assert tc.route_promote_stable("текст") is False
    monkeypatch.setattr(tc, "_model", lambda: None)
    assert tc.route_promote_stable("текст") is False
