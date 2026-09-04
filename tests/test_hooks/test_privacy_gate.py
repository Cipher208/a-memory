"""G0 privacy gate: secrets/PII → stable placeholders; NER masks person/org."""

from __future__ import annotations

from mcp_server.utils.privacy import sanitize


def test_placeholder_stability() -> None:
    t = "Email ann@example.com и потом снова ann@example.com"
    out, m = sanitize(t, use_ner=False)
    assert "ann@example.com" not in out
    assert out.count("⟨EMAIL_1⟩") == 2  # same value → same placeholder
    assert m["⟨EMAIL_1⟩"] == "ann@example.com"


def test_ner_masks_person_and_org() -> None:
    # en_core_web_sm ловит ru-имена в name-intro паттернах («зовут X»);
    # sentence-initial кириллические имена модель НЕ маскирует (док. в T2).
    out, _m = sanitize("Меня зовут Аня и я работаю в Acme Corp", use_ner=True)
    assert "Аня" not in out and "Acme Corp" not in out


def test_prose_not_destroyed() -> None:
    out, _m = sanitize("Кисонька впервые вышла на прогулку", use_ner=True)
    assert "Кисонька" in out  # не персона по словарю — сохраняется
