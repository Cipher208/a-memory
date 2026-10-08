"""Install the spaCy NER models the privacy gate needs — from inside the package.

Why this lives in the package instead of a `scripts/` file or a dependency.

The privacy gate (`hooks` ingest → `mcp_server/utils/privacy.py`) and the graph
miners load `en_core_web_sm` and `ru_core_news_sm`. Neither model is on PyPI:
`en-core-web-sm` returns 404 there, and `ru-core-news-sm` resolves to an unrelated
`999.9.9` placeholder. They can only be installed by wheel URL — and a wheel URL
**cannot be a published dependency**, because PyPI rejects any distribution whose
metadata carries a direct URL. Version 1.11.0 was refused with a bare
`HTTP 400 Bad Request` for exactly that reason, after its GitHub Release had
already gone out.

That leaves two mechanisms, and the first version of this fix picked the wrong one:

* a `[dependency-groups] ner` entry (PEP 735) is excluded from published metadata
  and is the uv-native way to say "developers need these" — but **pip does not
  install groups**, so CI broke, and nothing in the wheel could act on it;
* a module in the package ships to every user of `pip install a-memory`.

Both at once would mean two literal copies of the model URLs, and the drift
between them is the bug class this whole episode was made of. So the URLs live
**here**, once, and every consumer reads them from here: the console script below,
CI, a developer, and the `scripts/` wrapper kept for source checkouts.

Usage:

    a-memory-ner              # install (console script, present in the wheel)
    a-memory-ner --check      # report what is missing, install nothing
    python -m mcp_server.utils.ner_models --check

The models are optional. Without them the gate degrades and reports `degraded`
rather than failing, which is why a missing model is a warning and not an error.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

#: Where Explosion publishes model wheels; they are not on PyPI.
_BASE = "https://github.com/explosion/spacy-models/releases/download"

#: (import name, requirement spec) — the single source of truth for both halves.
#: Pinned to 3.8.0, the release built against spaCy 3.8.x.
MODELS: tuple[tuple[str, str], ...] = (
    (
        "en_core_web_sm",
        f"en-core-web-sm @ {_BASE}/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl",
    ),
    (
        "ru_core_news_sm",
        f"ru-core-news-sm @ {_BASE}/ru_core_news_sm-3.8.0/ru_core_news_sm-3.8.0-py3-none-any.whl",
    ),
)


def missing() -> list[tuple[str, str]]:
    """Return the models that cannot be imported, in declaration order."""
    absent = []
    for import_name, spec in MODELS:
        try:
            __import__(import_name)
        except ImportError:
            absent.append((import_name, spec))
    return absent


def install() -> int:
    """Install every missing model. Returns a process exit code."""
    absent = missing()
    print(f"declared models: {[name for name, _ in MODELS]}")
    if not absent:
        print("already installed: nothing to do")
        return 0

    print(f"missing: {[name for name, _ in absent]}")
    # Each spec is its own argv element: a spec is `name @ url` with spaces, so a
    # shell command line would split it in the wrong place.
    subprocess.check_call([sys.executable, "-m", "pip", "install", *[spec for _, spec in absent]])

    still = missing()
    if still:
        print(f"FAILED to install: {[n for n, _ in still]}", file=sys.stderr)
        return 1
    print("installed and importable")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="a-memory-ner",
        description="Install the optional spaCy NER models used by the privacy gate.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what is missing and exit non-zero, without installing",
    )
    args = parser.parse_args(argv)

    if args.check:
        absent = missing()
        print(f"declared models: {[name for name, _ in MODELS]}")
        if not absent:
            print("all present")
            return 0
        print(f"missing: {[name for name, _ in absent]}")
        print("--check given, not installing")
        return 1
    return install()


if __name__ == "__main__":
    raise SystemExit(main())
