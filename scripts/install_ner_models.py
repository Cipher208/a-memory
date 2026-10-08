#!/usr/bin/env python3
"""Install the spaCy NER models this project needs, from their declared URLs.

Why this exists instead of two lines in `dependencies`.

The privacy gate (`hooks` ingest → `mcp_server/utils/privacy.py`) and the graph
miners load `en_core_web_sm` and `ru_core_news_sm`. Neither model is on PyPI:
`en-core-web-sm` returns 404 there, and `ru-core-news-sm` resolves to an
unrelated `999.9.9` placeholder. So they can only be installed by wheel URL.

A wheel URL cannot be a published dependency. PyPI rejects any distribution
whose metadata carries a direct URL — version 1.11.0 was refused with a bare
`HTTP 400 Bad Request`, after its GitHub Release had already gone out. They are
therefore declared in `[dependency-groups] ner` (PEP 735), which is excluded from
published metadata by specification.

That leaves the problem this script solves: a dependency group is an environment
description, and pip does not install groups. CI needs the models present, or the
privacy tests fail — which is how the 1.11.0 packaging fix broke its own build.
So both CI and a developer read the group from `pyproject.toml` here, through one
implementation, and there is no second copy of the URLs to drift out of date.

    python scripts/install_ner_models.py            # install
    python scripts/install_ner_models.py --check    # report, install nothing
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
GROUP = "ner"


def _specs() -> list[str]:
    """Return the group's requirement strings, verbatim."""
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    specs = data.get("dependency-groups", {}).get(GROUP)
    if not specs:
        sys.exit(
            f"pyproject.toml has no [dependency-groups].{GROUP}. The spaCy NER models are "
            "required by the privacy gate; without this group nothing installs them."
        )
    return list(specs)


def _installed() -> set[str]:
    """Model names currently importable, by spaCy's own naming."""
    found = set()
    for module in ("en_core_web_sm", "ru_core_news_sm"):
        try:
            __import__(module)
        except ImportError:
            continue
        found.add(module.replace("_", "-"))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what is missing and exit non-zero, without installing",
    )
    args = parser.parse_args()

    specs = _specs()
    expected = {spec.split()[0] for spec in specs}
    missing = expected - _installed()

    print(f"declared in [dependency-groups].{GROUP}: {sorted(expected)}")
    if not missing:
        print("already installed: nothing to do")
        return 0

    print(f"missing: {sorted(missing)}")
    if args.check:
        print("--check given, not installing")
        return 1

    # Each spec is passed as its own argv element: a spec is `name @ url` and
    # contains spaces, so a shell command line would split it in the wrong place.
    subprocess.check_call([sys.executable, "-m", "pip", "install", *specs])

    still_missing = expected - _installed()
    if still_missing:
        print(f"FAILED to install: {sorted(still_missing)}", file=sys.stderr)
        return 1
    print("installed and importable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
