"""Every key under `limits:` is either read by code or declared inert — and checked.

Why this test exists.

`config.yaml` used to claim that every key in its `limits:` block was wired. It was
not: `l2_session_limit`, `l3_episodic_limit` and `l4_core_limit` are read by no
code at all, so an agent operator could set them, watch nothing happen, and have no
way to find out. The comment now says so in words — but words drift, and this
comment had already been wrong once.

So the words are checked in both directions:

* a key the comment calls inert must have no reader, and
* a key the comment calls live must have one.

Either side going stale fails here. If someone wires up `l4_core_limit`, this test
fails until the comment stops calling it dead — which is the point: the file must
not promise a setting that does nothing, nor deny one that works.

The reader pattern is the house's actual call shape, read off the code rather than
guessed: `config.get_limit("<key>")` delegates to `config.get("limits", "<key>")`
(`config.py:108`). A mention in a docstring is not a reader, and must not count as
one — `lifecycle/distiller.py` discusses these very keys without reading them.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "config.yaml"

# Where a key may be read from, without scanning the venv or the checkout's noise.
SOURCE_DIRS = ("mcp_server", "shared", "lifecycle", "features", "core", "hooks", "rag", "wiki")

_READ_BY_NOTHING = re.compile(r"read by nothing")
_ONLY_THESE_ARE = re.compile(r"only\s+(.+?)\s+are")


def _comment_line() -> str:
    """The comment line that names the inert keys.

    Failing loudly when it is absent is deliberate: a silently missing line would
    turn this test into one that cannot fail.
    """
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        if _READ_BY_NOTHING.search(line):
            return line
    raise AssertionError(
        "config.yaml no longer says which keys are read by nothing; this test cannot "
        "check a claim nobody makes. Restore the line or retire this test deliberately."
    )


def _backticked(text: str) -> set[str]:
    return set(re.findall(r"`([a-z0-9_]+)`", text))


def _keys_under_limits() -> set[str]:
    """Keys declared under the top-level `limits:` mapping."""
    keys: set[str] = set()
    inside = False
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        if re.match(r"^limits:\s*$", line):
            inside = True
            continue
        if inside:
            if line.strip() and not line.startswith((" ", "#", "\t")):
                break
            m = re.match(r"^\s{2}([a-z0-9_]+):", line)
            if m:
                keys.add(m.group(1))
    return keys


def _has_reader(key: str) -> bool:
    """Does any module CALL the config for this key?

    Only call shapes count. A docstring that names the key is not a reader — the
    distinction is the whole reason this guard can be trusted.

    Searched in Python rather than through a shell: the pattern contains both quote
    characters, and passing it through `bash -c "grep ... "` truncated it at the
    first double quote. That version reported `l1_buffer_size` as unread while
    `core/__init__.py:51` reads it, i.e. the guard failed on a case whose answer was
    already known. Checks get tried on known answers before they get believed.
    """
    call = re.compile(
        rf"""(get_limit\(\s*["']{re.escape(key)}["']"""
        rf"""|get\(\s*["']limits["']\s*,\s*["']{re.escape(key)}["'])"""
    )
    for d in SOURCE_DIRS:
        root = REPO / d
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            try:
                if call.search(path.read_text(encoding="utf-8")):
                    return True
            except (OSError, UnicodeDecodeError):
                continue
    return False


def test_the_comment_names_keys_that_actually_exist() -> None:
    """A comment naming a key that is not in the file documents nothing."""
    declared = _backticked(_comment_line())
    present = _keys_under_limits()
    unknown = declared - present
    assert not unknown, f"config.yaml calls {sorted(unknown)} inert, but those keys are not under `limits:`"


def test_keys_declared_inert_have_no_reader() -> None:
    """The claim being checked: these do nothing, and the file says so."""
    inert = _backticked(_comment_line()) & _keys_under_limits()
    assert inert, "the inert-key line names no key from `limits:` — nothing would be checked"

    working = sorted(k for k in inert if _has_reader(k))
    assert not working, (
        f"{working} are declared read-by-nothing but code now reads them. Update the comment above `limits:` in config.yaml (or restore the reader)."
    )


def test_keys_declared_live_have_a_reader() -> None:
    """The other direction: a key the comment calls wired must really be wired."""
    line = next(
        (candidate for candidate in CONFIG.read_text(encoding="utf-8").splitlines() if _ONLY_THESE_ARE.search(candidate)),
        None,
    )
    assert line, "config.yaml no longer names the keys that ARE read; this direction cannot be checked"

    live = _backticked(line) & _keys_under_limits()
    assert live, "the live-key line names no key from `limits:`"

    missing = sorted(k for k in live if not _has_reader(k))
    assert not missing, f"{missing} are declared read by code but nothing reads them — the file promises a setting that does nothing."
