from __future__ import annotations

"""Auto-routing signals for layer-less thoughts.

Agent layer stores identity/decisions/errors/personality (first-person
agent voice); user layer stores facts ABOUT the user.
"""

import re

_AGENT_SIGNALS = re.compile(
    r"\b(i|we)\s+(decided|choose|chose|prefer|fixed|broke|learned|mistook|failed|solved)\b"
    r"|\b(my|our)\s+(personality|style|approach|rule|policy)\b"
    r"|\b(decision_log|error_analysis|mistake|lesson_learned)\b",
    re.IGNORECASE,
)
_USER_SIGNALS = re.compile(
    r"\b(user|he|she|they|(?:mr|mrs|ms)?\.?\s?[A-Z][a-z]+)\s+(likes?|prefers?|wants?|hates?|said|asked|is)\b"
    r"|\b(the user)'?s?\b",
    re.IGNORECASE,
)


def _auto_route(text: str) -> str:
    """Route auto-layer thoughts: agent-voice content → agent, user facts → user."""
    agent_score = len(_AGENT_SIGNALS.findall(text))
    user_score = len(_USER_SIGNALS.findall(text))
    return "agent" if agent_score > user_score else "user"
