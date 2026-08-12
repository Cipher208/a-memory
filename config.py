"""
Universal Memory MCP Server
Two-layer unified memory: Layer 1 (user) + Layer 2 (agent identity)
"""

from pathlib import Path
from typing import Any, Self

import yaml


class Config:
    _instance: Self | None = None
    _data: dict[str, Any] = {}

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance

    def _load(self) -> None:
        config_path = Path(__file__).parent / "config.yaml"
        try:
            with open(config_path) as f:
                self._data = yaml.safe_load(f)
        except FileNotFoundError:
            self._data = {}

    def get(self, *keys: str, default: Any = None) -> Any:
        value = self._data
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key, default)
            else:
                return default
        return value

    def is_hook_enabled(self, layer: str, hook: str) -> bool:
        # Known hooks enabled by default
        known_hooks = {
            "user": [
                "message_received",
                "message_sent",
                "importance_gate",
                "emotion_trigger",
                "consolidation",
                "auto_context",
                "forgetting_ritual",
                "state_delta",
                "nightly",
                "retrieval_router",
                "conflict_resolver",
                "dream_buffer",
            ],
            "agent": [
                "error_occurred",
                "decision_made",
                "self_correction",
                "personality_shift",
                "emotion_context",
                "consolidation",
                "auto_context",
                "forgetting_ritual",
                "retrieval_router",
                "conflict_resolver",
                "emotion",
                "wiki_agent",
            ],
        }
        res = self.get("hooks", layer, hook, default=True) if hook in known_hooks.get(layer, []) else self.get("hooks", layer, hook, default=False)
        return bool(res)

    def is_feature_enabled(self, feature: str) -> bool:
        return bool(self.get("features", feature, default=False))

    def get_wiki_types(self, layer: str) -> list[str]:
        res: Any = self.get("wiki", layer, default=[])
        if not isinstance(res, list):
            return []
        return [str(x) for x in res]

    def get_limit(self, key: str) -> int:
        return int(self.get("limits", key, default=0))

    def get_forgetting(self, key: str) -> float:
        return float(self.get("forgetting", key, default=0.0))


config = Config()
