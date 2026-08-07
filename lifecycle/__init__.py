"""
Lifecycle Module - forgetting, emotion trigger, consolidation
"""

from .consolidation import ConsolidationEngine
from .emotion_trigger import EmotionTrigger
from .forgetting import ForgettingSystem

__all__ = ["ConsolidationEngine", "EmotionTrigger", "ForgettingSystem"]
