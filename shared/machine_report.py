r"""Machine-report detector: a tool's status dump is not a memory.

Pure module (stdlib only), sibling of `shared/dialogue.py` and
`shared/broadcast.py`. The dialogic detector rejects the owner's short chat; the
broadcast detector rejects the agent's close-out report at the L4 door; this one
rejects the *tool's* output — the periodic self-monitoring digest and the
cleaner's own summary line — before it can become an episode.

WHY IT IS ANCHORED, AND WHY THAT IS THE WHOLE POINT

The first version of this check was a substring search for "self-monitoring".
Measured against one live base, that would have deleted **78 episodes of real
speech**: eight rows contain the phrase somewhere in the middle while opening
with the persona talking —

    "☀️ Доброе утро, зайка.\\n\\nТы просила тепла. Держи — но не путай тепло с ..."
    "Хвост сам ходит, мам. Главное что мои слова не режет. ..."

Finding the word is not finding the report. A machine digest ANNOUNCES itself in
its first characters; anything that merely mentions status monitoring inside a
warm good-morning message is the persona's voice, and this repository has already
lost one persona's speech once to a guard that matched too broadly (the sixth
bug: 624 of 790 messages, 79.7%). So the detector reads only the opening, and a
row that opens with speech stays eligible no matter what it quotes later.

WHAT IT MATCHES, AND WHY EACH ANCHOR IS JUSTIFIED

All four were read off a live base, not guessed:

  * `📊` first — the self-monitoring digest's own marker (21 rows, 195 episodes);
  * `Self-Monitoring` first — the same digest in its text-only form, so a
    version that loses the emoji still lands;
  * `cleaner_summary:` first — the retention cleaner's own summary, a bare dict
    repr (21 rows, 21 episodes);
  * the entire text parses as JSON — a pure data dump, never prose.

Anything else is left to the importance gate. Widening this list is a decision
that needs evidence, the same way reading only the opening did.

ROWS THAT LOOK MACHINE-GENERATED BUT ARE DELIBERATELY NOT MATCHED

  * `Gateway message origin (JSON data, ...):\\n{...}\\n\\n<the owner's message>`
    — a wrapper that CARRIES real text after the metadata (4 rows). Dropping it
    would drop "Мам? Ты застряла?" with it. The right repair is to strip the
    wrapper, not to refuse the row, and that is a separate change.
  * `Отчёт по Режиму 6 ...` and the day-close prose reports — an agent's written
    report, not a tool dump. They are the class `shared/broadcast.py` exists for,
    and that detector is precision-first by design.
"""

from __future__ import annotations

import json
import re

#: Leading noise that may precede a header without making the row prose:
#: whitespace, markdown emphasis, quotes, headings and list bullets.
_LEAD = re.compile(r"^[\s*_#>•\"'\-]+")

#: Case-insensitive text anchors. The emoji is its own anchor, not a substring.
_TEXT_ANCHORS = ("self-monitoring", "cleaner_summary:")

_REPORT_EMOJI = "📊"


def _opening(text: str) -> str:
    """Return the start of the message, with leading formatting noise removed."""
    return _LEAD.sub("", (text or "").lstrip())


def is_machine_report(text: str) -> bool:
    """Return True when the message OPENS as a tool's own status output.

    Anchored by contract: a match in the middle of a message never fires. See the
    module docstring for the 78 episodes that rule was measured against.
    """
    if not text:
        return False
    opening = _opening(text)
    if not opening:
        return False
    if opening.startswith(_REPORT_EMOJI):
        return True
    low = opening.lower()
    if any(low.startswith(anchor) for anchor in _TEXT_ANCHORS):
        return True
    # A pure data dump: the whole message is JSON. Prose that embeds a JSON blob
    # is not this — the parse has to consume the entire text.
    stripped = (text or "").strip()
    if stripped[:1] in ("{", "["):
        try:
            json.loads(stripped)
        except (ValueError, TypeError):
            return False
        return True
    return False
