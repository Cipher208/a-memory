from pydantic import BaseModel, Field
from typing import Dict, List, Any

class EmotionResult(BaseModel):
    """Result of emotional analysis of a message."""
    trigger_type: str
    score: float
    metadata: Dict[str, Any] = Field(default_factory=dict)

class PhrasePattern(BaseModel):
    """Phrase pattern with metadata for regex matching."""
    pattern: str
    emotion: str
    score: float
    lang: str

class EmotionMarkerConfig(BaseModel):
    """Configuration for emotional markers loaded from assets."""
    markers: Dict[str, List[str]]
    phrases: List[PhrasePattern]
    emojis: Dict[str, List[str]]
