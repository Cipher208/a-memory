"""Wiki secret detection — scan page content for well-known secret formats.

Warn-only: the caller (WikiManager.add / _sync_one_file) logs a WARNING per
finding but never blocks the write. Pure function; no IO, no logging here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class SecretFinding:
    kind: str  # "github_pat" | "api_key" | "pem_private_key"
    location: str  # "body"


GH_PAT_RE = re.compile(r"\b(ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})\b")
API_KEY_RE = re.compile(r"\b(sk-[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_\-]{35}|AKIA[0-9A-Z]{16})\b")
PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def scan_secrets(text: str) -> list[SecretFinding]:
    """Return a finding per detected secret format. Empty for clean text."""
    findings: list[SecretFinding] = []
    for kind, pattern in (
        ("github_pat", GH_PAT_RE),
        ("api_key", API_KEY_RE),
        ("pem_private_key", PEM_RE),
    ):
        if pattern.search(text):
            findings.append(SecretFinding(kind=kind, location="body"))
    return findings
