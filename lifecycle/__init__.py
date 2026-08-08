"""
Lifecycle Module - forgetting, emotion trigger, consolidation
"""

from .consolidation import ConsolidationEngine
from .emotion import EmotionTrigger, EmotionEngine
from .forgetting import ForgettingSystem

__all__ = ["ConsolidationEngine", "EmotionTrigger", "EmotionEngine", "ForgettingSystem"]
