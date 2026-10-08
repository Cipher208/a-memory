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

import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import InvalidRequirement, Requirement

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


def _declared_requirements() -> list[tuple[str, str]]:
    """Every requirement named in pyproject.toml, with the table it came from.

    Includes `[dependency-groups]`, which is NOT published metadata (PEP 735) and
    is therefore not a PyPI risk — it is scanned so that a URL appearing there is
    still noticed as a second copy of something the package already declares.
    """
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    found = [("dependencies", spec) for spec in project.get("dependencies", [])]
    for extra, specs in (project.get("optional-dependencies") or {}).items():
        found += [(f"optional-dependencies.{extra}", spec) for spec in specs]
    for group, specs in (data.get("dependency-groups") or {}).items():
        found += [(f"dependency-groups.{group}", spec) for spec in specs]
    return found


def test_published_metadata_carries_no_direct_reference() -> None:
    """PyPI refuses a distribution whose metadata holds a direct URL.

    1.11.0 failed its upload with a bare `HTTP 400 Bad Request` — the GitHub
    Release was published, the wheel was not. The cause was two spaCy model
    dependencies declared as `name @ https://...`, which reached `Requires-Dist`.
    Neither model is on PyPI (`en-core-web-sm` 404s; `ru-core-news-sm` is an
    unrelated placeholder), so there was no way to name them instead.

    What made it invisible is the part worth keeping: the project also declared
    `[tool.hatch.metadata] allow-direct-references = true`, so the build
    *succeeded* and only the upload was refused. The flag is gone on purpose —
    without it, hatchling refuses to build a wheel carrying a URL dependency,
    which is a failure at the point where it is cheap.

    This test asserts both halves, so neither can come back alone.
    """
    bad: list[str] = []
    for table, spec in _published_requirements():
        try:
            requirement = Requirement(spec)
        except InvalidRequirement as exc:  # a malformed spec is its own release blocker
            bad.append(f"{table}: {spec!r} is not a valid requirement ({exc})")
            continue
        if requirement.url:
            bad.append(f"{table}: {spec!r}")
    assert not bad, (
        "these requirements carry a direct URL and would make the upload fail with "
        f"HTTP 400 after the release was already published: {bad}. Move them out of "
        "published metadata and install them from a package module instead."
    )


def test_the_flag_that_hid_it_is_still_gone() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    metadata = data.get("tool", {}).get("hatch", {}).get("metadata", {})
    assert not metadata.get("allow-direct-references"), (
        "allow-direct-references lets hatchling emit URL dependencies into Requires-Dist, "
        "which PyPI rejects at upload time — the build passes and the release does not"
    )


def _published_requirements() -> list[tuple[str, str]]:
    """The requirements that end up in `Requires-Dist`, and so in an upload.

    `[dependency-groups]` is deliberately excluded: PEP 735 keeps it out of
    published metadata, so a URL there cannot fail an upload — only duplicate a
    list, which the duplicate check in the model-URL test covers.
    """
    return [(table, spec) for table, spec in _declared_requirements() if not table.startswith("dependency-groups")]


def _model_urls() -> list[str]:
    """Model URLs as the package declares them, importable without spaCy installed."""
    from mcp_server.utils.ner_models import MODELS

    return [spec for _import_name, spec in MODELS]


def test_ner_models_declare_urls_that_are_not_project_dependencies() -> None:
    """One list of model URLs, in the package — never a second copy for uv.

    The first attempt put them in `[dependency-groups] ner`, which is the uv-native
    way to say "developers need these" and is excluded from published metadata. It
    also broke CI (pip does not install groups) and gave a `pip install a-memory`
    user no way to run it at all, because `scripts/` is not in the wheel. Keeping
    both would mean two literal copies of these URLs drifting apart.
    """
    urls = _model_urls()
    assert len(urls) == 2, urls
    for spec in urls:
        requirement = Requirement(spec)
        assert requirement.url, f"{spec!r} should be a direct URL"
        assert requirement.name in {"en-core-web-sm", "ru-core-news-sm"}, requirement.name

    declared = {spec for _table, spec in _declared_requirements()}
    duplicated = declared.intersection(urls)
    assert not duplicated, f"model URLs are declared in pyproject.toml as well: {duplicated}"


def test_ner_models_ship_an_entry_point_that_reaches_a_pip_user() -> None:
    """`scripts/` is not in the wheel, so the console script is the only path."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    target = scripts.get("a-memory-ner")
    assert target == "mcp_server.utils.ner_models:main", scripts

    module, _, attribute = target.partition(":")
    assert (REPO_ROOT / Path(*module.split(".")).with_suffix(".py")).is_file(), module
    assert attribute == "main"


def test_the_install_script_holds_no_copy_of_the_urls() -> None:
    """The `scripts/` wrapper must delegate, or the URLs exist twice again."""
    text = (REPO_ROOT / "scripts" / "install_ner_models.py").read_text(encoding="utf-8")
    assert "spacy-models/releases" not in text, (
        "scripts/install_ner_models.py repeats the model URLs; it should import them from mcp_server.utils.ner_models so there is one source of truth"
    )


def _identity_check(name: str, email: str, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the identity guard against an identity set in a throwaway repository.

    The identity goes into the temporary repo's **local** config, which is where
    `.git/config` really decides authorship and which nothing in the ambient
    environment can outrank. Setting it through `GIT_CONFIG_COUNT` instead made the
    first version of these tests fail under pre-commit: the surrounding
    environment won, and the guard reported the *host* identity rather than the one
    under test. Environment variables are also scrubbed for the same reason.

    Neutral addresses throughout — `example.com` — so this file itself carries no
    address that must stay out of the repository.
    """
    with tempfile.TemporaryDirectory() as tmp:
        git = shutil.which("git") or "git"
        clean = {key: value for key, value in os.environ.items() if not key.startswith("GIT_") and key != "A_MEMORY_ALLOW_ANY_COMMIT_IDENTITY"}
        clean.update(env_overrides or {})

        subprocess.run([git, "init", "-q", tmp], check=True, capture_output=True, env=clean)
        subprocess.run([git, "config", "user.name", name], cwd=tmp, check=True, env=clean)
        subprocess.run([git, "config", "user.email", email], cwd=tmp, check=True, env=clean)

        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check_commit_identity.py")],
            cwd=tmp,
            capture_output=True,
            text=True,
            env=clean,
            check=False,
        )


def _identity_guard_module():
    """Load `scripts/check_commit_identity.py` so tests read its real constants.

    Importing beats repeating: the assertions below check that the guard's message
    advertises the guard's own allowlist, which they cannot do from a copy.

    It also keeps a bare domain literal out of this file. `"<domain>" in text` is
    the shape of CodeQL's `py/incomplete-url-substring-sanitization`, which flagged
    the literal that used to be here — an assert is not sanitization, but the
    pattern is identical and the alert is real noise. A value read from the module
    cannot be mistaken for one.
    """
    import importlib.util

    path = REPO_ROOT / "scripts" / "check_commit_identity.py"
    spec = importlib.util.spec_from_file_location("check_commit_identity", path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_identity_guard_blocks_an_unrecognised_address() -> None:
    """The guard fails, not warns: an address in published history is permanent."""
    result = _identity_check("Someone", "someone@example.com")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not a public account" in result.stderr
    # The remedy has to be in the message, or the hook only blocks.
    assert "git config user.email" in result.stderr
    # And it must name the allowlist the guard actually applies.
    advertised = _identity_guard_module().ALLOWED_EMAIL_SUFFIXES[0]
    assert advertised in result.stderr


def test_identity_guard_is_an_allowlist_not_a_list_of_known_bad_names() -> None:
    """An address nobody has thought of yet must still be refused.

    A denylist would pass this one; that is the point. Naming the forbidden values
    in the script made gitleaks reject the commit — correctly, since those values
    are the very thing that must not be written down.
    """
    result = _identity_check("Unknown", "unknown-operator@some-other-host.example")
    assert result.returncode == 1, result.stdout + result.stderr


def test_identity_guard_passes_a_github_noreply_identity() -> None:
    suffix = _identity_guard_module().ALLOWED_EMAIL_SUFFIXES[0]
    result = _identity_check("Cipher208", f"269750686+Cipher208{suffix}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Cipher208" in result.stdout


def test_identity_guard_override_is_deliberate() -> None:
    """An override exists — but only when asked for explicitly, by name."""
    result = _identity_check(
        "Someone",
        "someone@example.com",
        {"A_MEMORY_ALLOW_ANY_COMMIT_IDENTITY": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "allowed by" in result.stdout


def test_the_identity_guard_is_wired_into_pre_commit() -> None:
    """A guard nobody runs is a comment. It must be registered as a hook."""
    import yaml

    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    local = next(r for r in config["repos"] if r.get("repo") == "local")
    hooks = {h["id"]: h for h in local["hooks"]}
    assert "commit-identity" in hooks, sorted(hooks)
    assert "check_commit_identity" in hooks["commit-identity"]["entry"]
    assert hooks["commit-identity"]["always_run"] is True
