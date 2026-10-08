"""Release hygiene: the published body must not carry a persona identity.

Why this test exists rather than a careful reading.

`release.yml` used to publish releases with `generate_release_notes: true`, which
composes the body from PR titles and commit authors. The draft it left behind
closed with a real committer identity — a name and a handle that are not meant to
appear anywhere in this repository's public output. Fixing the workflow to build
the body from `CHANGELOG.md` removed that particular source, but the same leak
came straight back twice within the same change:

* the CHANGELOG entry explaining the fix *quoted* the identity it was removing;
* the workflow comment did the same, in a second file.

Both were caught by reading, and reading was the thing that had already failed.
So the rule lives here now: whatever `release.yml` extracts and publishes is
checked against the anonymizer's own persona dictionary, in the form it will be
published. The dictionary is read from `privacy._ru_personas()` — the same
frozenset the ru privacy tier masks with — so this test cannot drift from the
list it is protecting.

The identity list itself is not the bug and is not touched: `config.yaml`
`rag.ru_personas`, `rag/synonyms.py` and the privacy tests *are* the feature that
hides these names. This test only forbids them from reaching release output.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mcp_server.utils.privacy import _ru_personas

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# The extraction in .github/workflows/release.yml, kept identical on purpose.
# If that regex changes, this test must be changed with it — a mismatch would
# make the guard check a body the release never publishes.
_SECTION = re.compile(
    r"^## \[(?P<version>[^\]]+)\][^\n]*\n(.*?)(?=^## \[|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _release_body(version: str) -> str:
    """Return the section `release.yml` would publish for `version`."""
    text = CHANGELOG.read_text(encoding="utf-8")
    for match in _SECTION.finditer(text):
        if match.group("version") == version:
            return match.group(2).strip()
    raise AssertionError(f"CHANGELOG.md has no '## [{version}]' section")


def _versions() -> list[str]:
    return [
        m.group("version")
        for m in _SECTION.finditer(CHANGELOG.read_text(encoding="utf-8"))
        if not m.group("version").lower().startswith("unreleased")
    ]


def _occurrences(text: str, persona: str) -> list[str]:
    """Whole-word, case-sensitive hits — `Eli` must not match `Eliot`."""
    return [m.group(0) for m in re.finditer(rf"(?<![A-Za-zА-Яа-яЁё]){re.escape(persona)}(?![A-Za-zА-Яа-яЁё])", text)]


def test_versions_are_discovered() -> None:
    # Guards the guard: a broken regex would silently check nothing at all.
    versions = _versions()
    assert versions, "no released version sections found in CHANGELOG.md"
    assert "1.11.0" in versions


def test_1_11_0_body_is_extractable_and_non_empty() -> None:
    # The workflow fails the release when this is empty; catch it before a tag.
    body = _release_body("1.11.0")
    assert body.startswith("### ")
    assert len(body) > 1000


@pytest.mark.parametrize("version", _versions())
def test_release_body_carries_no_persona_identity(version: str) -> None:
    body = _release_body(version)
    leaked = {p: hits for p in _ru_personas() if (hits := _occurrences(body, p))}
    assert not leaked, (
        f"release body for v{version} carries persona identities {sorted(leaked)}; "
        "they are published to GitHub Releases and PyPI, and the changelog is not "
        "the place to quote the name being removed"
    )


def test_persona_dictionary_is_not_empty() -> None:
    personas = _ru_personas()
    assert len(personas) >= 10, f"anonymizer dictionary shrank to {len(personas)}: {sorted(personas)}"
    # The two that leaked into release text during this change, by construction.
    assert "Murat" in personas and "Эли" in personas
