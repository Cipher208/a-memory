"""Code policy: scripts must not hardcode a developer's home directory.

The repository is public. A literal ``/home/<user>/...`` path leaks the local
username and directory layout, and it is wrong the moment anyone clones the
project elsewhere. Paths derived from the checkout (``Path(__file__)``) or from
``Path.home()`` for genuinely user-scoped state are the supported forms.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Maintenance scripts live next to the package they operate on and run manually
# (no cron, no service). Their home-directory references must resolve at runtime.
SCRIPTS = [
    "scripts/docs_to_wiki.py",
    "scripts/purge_l3_legacy.py",
    "scripts/sync_skills.py",
]


def _string_constants(tree: ast.AST) -> list[tuple[int, str]]:
    """Return (lineno, value) for every string literal in the module."""
    return [(node.lineno, node.value) for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def test_no_home_directory_literals_in_scripts() -> None:
    offenders: list[str] = []
    for relative in SCRIPTS:
        source = (ROOT / relative).read_text(encoding="utf-8")
        for lineno, value in _string_constants(ast.parse(source)):
            # Only absolute home paths are the defect. Prose such as
            # "~/skills-ssot" or a mention of "$HOME" is fine.
            if value.startswith("/home/"):
                offenders.append(f"{relative}:{lineno}: {value}")
    assert offenders == [], "hardcoded home directory path(s):\n" + "\n".join(offenders)


def test_scripts_derive_repo_root_from_checkout() -> None:
    """Every script that extends sys.path must locate the repo via __file__."""
    for relative in SCRIPTS:
        source = (ROOT / relative).read_text(encoding="utf-8")
        if "sys.path.insert" not in source:
            continue
        assert "__file__" in source, f"{relative} extends sys.path without anchoring to __file__"
