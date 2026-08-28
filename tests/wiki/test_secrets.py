"""Tests for wiki.secrets (secret detection scanner)."""
from __future__ import annotations

from wiki.secrets import scan_secrets


def test_detects_github_pat():
    findings = scan_secrets("token: ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    assert any(f.kind == "github_pat" for f in findings)


def test_detects_api_key():
    findings = scan_secrets("key=sk-abcdefghijklmnopqrstuvwxyz1234567890")
    assert any(f.kind == "api_key" for f in findings)


def test_detects_pem_private_key():
    findings = scan_secrets("-----BEGIN RSA PRIVATE KEY-----\nbase64\n-----END RSA PRIVATE KEY-----")
    assert any(f.kind == "pem_private_key" for f in findings)


def test_clean_text_no_findings():
    assert scan_secrets("just a normal wiki note about the weather") == []


def test_short_key_no_false_positive():
    # sk-abc is too short to be a real API key
    assert scan_secrets("key=sk-abc") == []


def test_multiple_kinds_one_text():
    findings = scan_secrets("ghp_abcdefghijklmnopqrstuvwxyz0123456789 and sk-abcdefghijklmnopqrstuvwxyz1234567890")
    kinds = {f.kind for f in findings}
    assert {"github_pat", "api_key"} <= kinds
