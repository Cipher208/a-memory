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


# --- F1: the credential tier runs inside sanitize(), as its FIRST tier ---


def test_sanitize_masks_credentials_and_keeps_prose() -> None:
    """Every credential shape the gate must catch, and one sentence it must not touch.

    `sanitize()` used to run only `_PII_PATTERNS` + persona + NER; the credential
    tier lived in `strip_secrets()`, whose sole caller is `memory_remember`. So the
    L0 door, the replay and the refusal log all stored `sk-…`/`ghp_…` verbatim.

    The fixtures are assembled at runtime: a literal shaped like a credential trips
    the repository's own secret scanner on every push, and a test suite must not
    teach the scanner to ignore its own directory.
    """
    sk = "sk-" + "a" * 32
    ghp = "ghp_" + "A" * 36
    aws = "AKIA" + "IOSFODNN7EXAMPLE"
    jwt_head = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    jwt = jwt_head + "." + "eyJzdWIiOiIxIn0"
    pem_head = "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5
    pem_tail = "-" * 5 + "END RSA PRIVATE KEY" + "-" * 5
    pem_body = "MIIEowIBAAKCAQEA" + "1234567890abcdef"

    text = f"ключ {sk}\nтокен {ghp}\naws {aws}\nAuthorization: Bearer {jwt}\n{pem_head}\n{pem_body}\n{pem_tail}"
    out, mapping = sanitize(text, use_ner=False)

    for secret in (sk, ghp, aws, jwt_head, pem_body):
        assert secret not in out, f"credential survived sanitize: {secret!r}"
    assert "⟨CREDENTIAL_" in out
    assert any("ghp_" in value for value in mapping.values())

    plain = "Обычная фраза без секретов и без почты."
    assert sanitize(plain, use_ner=False)[0] == plain


def test_credential_tier_runs_before_the_pii_tier() -> None:
    """Order is load-bearing: the PHONE regex eats the digits of a ghp_ token.

    `ghp_…1234567890` through `_PII_PATTERNS` first becomes
    `ghp_…⟨PHONE_1⟩` — the credential is then unrecognisable to the tier that
    was supposed to mask it, and a fragment of the token stays in storage.
    """
    token = "ghp_" + "A" * 26 + "1234567890"
    out, _m = sanitize("ключ " + token, use_ner=False)

    assert "⟨CREDENTIAL_1⟩" in out
    assert "⟨PHONE_" not in out, f"the PII tier ran first: {out!r}"
    assert token not in out


def test_private_block_is_masked_whole() -> None:
    """`<private>…</private>` is removed entirely, not partially.

    The tag had no consumer anywhere in the repo — nothing ever stripped it, so
    the markup and its contents went to storage verbatim. It is a credential-tier
    shape on purpose: private content must not be persisted at all, and the whole
    block (tags included) is what the placeholder stands for.
    """
    out, mapping = sanitize("до <private>заметка</private> после", use_ner=False)

    assert out == "до ⟨CREDENTIAL_1⟩ после"
    assert mapping["⟨CREDENTIAL_1⟩"] == "<private>заметка</private>"
