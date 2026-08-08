from pydantic import BaseModel, Field
from typing import Any

class EmotionResult(BaseModel):
    """Result of emotional analysis of a message."""
    trigger_type: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)

class PhrasePattern(BaseModel):
    """Phrase pattern with metadata for regex matching."""
    pattern: str
    emotion: str
    score: float
    lang: str

class EmotionMarkerConfig(BaseModel):
    """Configuration for emotional markers loaded from assets."""
    markers: dict[str, list[str]]
    phrases: list[PhrasePattern]
    emojis: dict[str, list[str]]
