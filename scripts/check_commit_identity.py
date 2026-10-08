#!/usr/bin/env python3
"""Refuse to commit under an identity that is not the project's.

Why this exists. During the 1.11.0 release the same identity leak appeared three
times — in a CHANGELOG entry, a CI comment, and pasted assertion output — and
grepping caught it each time, which is discipline rather than a boundary. Commit
authorship is the same shape of problem: `.git/config` decides it, not intent, and
a fresh `git init` inherits the global identity, which on this host is a personal
address.

Why an allowlist and not a denylist. The first version of this script listed the
addresses to refuse — and was blocked by gitleaks, correctly: the forbidden values
are house infrastructure, so a file that names them is itself the leak. That
happened while writing the guard for that exact leak. An allowlist needs no
secret: a commit is acceptable if its address is a public identity, and anything
unrecognised is refused. It is also stricter — an address nobody has thought of
yet is caught too.

Run as a pre-commit hook:  python3 scripts/check_commit_identity.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

#: Email domains that identify a public account rather than a person's mailbox.
#: Deliberately a short, public list — nothing here is a secret.
ALLOWED_EMAIL_SUFFIXES = (
    "@users.noreply.github.com",  # GitHub's per-account noreply address
)

#: A commit may legitimately carry another identity, e.g. an import of upstream
#: history. Set to "1" deliberately — never as a workaround for a failing check.
ALLOW_ENV = "A_MEMORY_ALLOW_ANY_COMMIT_IDENTITY"


def _git(*args: str) -> str:
    git = shutil.which("git")
    if git is None:  # no git: there is no identity to read, and nothing to block
        return ""
    result = subprocess.run([git, *args], capture_output=True, text=True, check=False)
    return result.stdout.strip()


def main() -> int:
    if _git("rev-parse", "--is-inside-work-tree") != "true":
        return 0  # not a repository: nothing to check

    name = _git("config", "user.name")
    email = _git("config", "user.email")

    if not email:
        print("commit identity: no user.email configured — git will refuse anyway")
        return 0

    if any(email.endswith(suffix) for suffix in ALLOWED_EMAIL_SUFFIXES):
        print(f"commit identity: {name} <{email}>")
        return 0

    if os.environ.get(ALLOW_ENV) == "1":
        print(f"commit identity: {name} <{email}> allowed by {ALLOW_ENV}")
        return 0

    print(
        f"commit identity is not a public account: {name} <{email}>\n"
        f"  allowed suffixes: {', '.join(ALLOWED_EMAIL_SUFFIXES)}\n"
        "  A personal address in a published history cannot be taken back. Set the\n"
        "  repository identity instead, using the GitHub noreply address:\n"
        "    git config user.name  <your-github-login>\n"
        "    git config user.email <id>+<your-github-login>@users.noreply.github.com\n"
        f"  Override only with intent: {ALLOW_ENV}=1",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
