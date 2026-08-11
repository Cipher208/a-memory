"""
Universal Memory MCP Server
Two-layer unified memory: Layer 1 (user) + Layer 2 (agent identity)
"""

from pathlib import Path

import yaml


class Config:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance

    def _load(self):
        config_path = Path(__file__).parent / "config.yaml"
        try:
            with open(config_path) as f:
                self._data = yaml.safe_load(f)
        except FileNotFoundError:
            self._data = {}

    def get(self, *keys, default=None):
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
        if hook in known_hooks.get(layer, []):
            return self.get("hooks", layer, hook, default=True)
        return self.get("hooks", layer, hook, default=False)

    def is_feature_enabled(self, feature: str) -> bool:
        return self.get("features", feature, default=False)

    def get_wiki_types(self, layer: str) -> list:
        return self.get("wiki", layer, default=[])

    def get_limit(self, key: str) -> int:
        return self.get("limits", key, default=0)

    def get_forgetting(self, key: str) -> float:
        return self.get("forgetting", key, default=0.0)


config = Config()
