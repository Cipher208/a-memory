"""Fractional index keys (S1 order_key) — the CC0 fractional-indexing pattern on hex digits.

A key is a string of hex digits; the lexicographic order of strings = record order.
No external dependencies (S12: vendored minimum instead of a package).
"""

from __future__ import annotations

_START = "08000000"
_DIGITS = "0123456789abcdef"
_MID = "8"


def midpoint(a: str | None = None, b: str | None = None) -> str:
    """Return the key lexicographically after ``a`` and before ``b``.

    - midpoint(None, None) — the start key;
    - midpoint(a, None) — the one after a (monotonic append);
    - midpoint(None, b) — the one before b;
    - midpoint(a, b) — between a and b when a < b; when the depth is
      exhausted the key is extended (a + "8") rather than degenerating.

    Raises:
        ValueError: a >= b or an empty interval (a is a prefix of b made of zeros).

    """
    if not a and not b:
        return _START
    if not a:
        return _decrement(b or "")
    if not b:
        return _increment(a)
    if a >= b:
        msg = f"midpoint: a >= b ({a!r} >= {b!r})"
        raise ValueError(msg)
    for i in range(max(len(a), len(b))):
        da = _DIGITS.index(a[i]) if i < len(a) else 0
        db = _DIGITS.index(b[i]) if i < len(b) else 0
        if db > da:
            if db - da >= 2:
                # a[:i] padded with zeros when i is past the end of a (a is a prefix of b)
                return a[:i].ljust(i, "0") + _DIGITS[(da + db) // 2]
            # adjacent digits: go one level deeper past the end of a
            return a + _MID
    msg = f"no key between {a!r} and {b!r} (adjacent zero-prefixes)"
    raise ValueError(msg)


def _increment(a: str) -> str:
    """Return the key after a: bump the last digit; a tail of 'f' → extend the string."""
    if not a:
        return _START
    if a[-1] != "f":
        return a[:-1] + _DIGITS[_DIGITS.index(a[-1]) + 1]
    return a + "0"


def _decrement(b: str) -> str:
    """Return the key before b; at the floor ('0' tail) step up one digit."""
    if not b:
        msg = "cannot decrement empty key"
        raise ValueError(msg)
    if b[-1] != "0":
        return b[:-1] + _DIGITS[_DIGITS.index(b[-1]) - 1]
    return _decrement(b[:-1]) + "f"
