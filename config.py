"""Global Configuration Management for Ariel-Memory.

Handles environment-based settings, feature flags, and hook orchestration
using a singleton pattern to ensure consistent state across the server.

Config resolution order:
  1. $MCP_CONFIG_PATH (per-agent overrides — one config per data dir)
  2. repo-root config.yaml (shared default)

When a per-agent copy lacks keys present in the repo default (e.g. after
an update ships new settings), a startup warning lists them — defaults
still apply, but the copy is worth refreshing.
"""

import logging
import os
from pathlib import Path
from typing import Any, Self

import yaml

logger = logging.getLogger(__name__)


def _missing_keys(loaded: dict[str, Any], reference: dict[str, Any], prefix: str = "") -> list[str]:
    """Keys present in reference but absent from loaded (deep)."""
    missing: list[str] = []
    for key, value in reference.items():
        full = f"{prefix}{key}"
        if key not in loaded:
            missing.append(full)
        elif isinstance(value, dict) and isinstance(loaded.get(key), dict):
            missing.extend(_missing_keys(loaded[key], value, prefix=full + "."))
    return missing


class Config:
    _instance: Self | None = None
    _data: dict[str, Any] = {}

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance

    @staticmethod
    def config_path() -> Path:
        """Per-agent config wins; repo-root config.yaml is the shared default."""
        override = os.environ.get("MCP_CONFIG_PATH")
        if override:
            p = Path(override)
            if p.is_file():
                return p
        return Path(__file__).parent / "config.yaml"

    def _load(self) -> None:
        path = self.config_path()
        try:
            with open(path) as f:
                self._data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            self._data = {}
        self._warn_on_drift(path)

    def _warn_on_drift(self, path: Path) -> None:
        """Warn when a per-agent copy lacks keys added by newer defaults."""
        repo_default = Path(__file__).parent / "config.yaml"
        try:
            if not path.is_file() or path.resolve() == repo_default.resolve():
                return
            with open(repo_default) as f:
                reference = yaml.safe_load(f) or {}
            missing = _missing_keys(self._data, reference)
            if missing:
                logger.warning(
                    "Config %s is missing %d key(s) present in the repo default (defaults still apply): %s — consider refreshing from config.yaml",
                    path,
                    len(missing),
                    ", ".join(missing[:10]),
                )
        except Exception:
            logger.debug("config drift check failed", exc_info=True)

    def get(self, *keys: str, default: Any = None) -> Any:
        value = self._data
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key, default)
            else:
                return default
        return value

    def is_hook_enabled(self, layer: str, hook: str) -> bool:
        """Single source of truth: hooks.<layer>.<hook> in config.yaml.

        Default True — a hook not mentioned in yaml runs; disable explicitly
        with `false`. No code-side duplicate list to drift.
        """
        return bool(self.get("hooks", layer, hook, default=True))

    def is_feature_enabled(self, feature: str) -> bool:
        return bool(self.get("features", feature, default=False))

    def get_limit(self, key: str) -> int:
        return int(self.get("limits", key, default=0))

    def get_forgetting(self, key: str) -> float:
        return float(self.get("forgetting", key, default=0.0))


config = Config()
