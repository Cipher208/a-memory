from __future__ import annotations

"""
Adaptive Threshold Management — EMA-based dynamic importance filtering.
"""

import logging
import time

from shared.connection import connection_manager

logger = logging.getLogger(__name__)


class AdaptiveThresholdManager:
    """Manages dynamic importance threshold using EMA."""

    DEFAULT_THRESHOLD = 0.3
    ALPHA = 0.1  # Smoothing factor (higher = faster adaptation)
    MIN_THRESHOLD = 0.1
    MAX_THRESHOLD = 0.6

    def __init__(self, key: str = "importance_threshold_ema"):
        self.key = key
        self._current_value: float | None = None

    async def get_threshold(self) -> float:
        """Get current EMA threshold from cache or DB."""
        if self._current_value is not None:
            return self._current_value

        try:
            conn = await connection_manager.get("memory.db")
            row = await (await conn.execute("SELECT value FROM preferences WHERE key=?", (self.key,))).fetchone()

            if row:
                self._current_value = float(row[0])
            else:
                self._current_value = self.DEFAULT_THRESHOLD
                await self._save(self._current_value)
        except Exception as e:
            logger.warning(f"Failed to load adaptive threshold from DB, using default: {e}")
            self._current_value = self.DEFAULT_THRESHOLD

        # Update prometheus metric
        from shared.metrics import metrics

        metrics.current_importance_threshold.set(self._current_value)

        return self._current_value

    async def update(self, new_score: float) -> float:
        """Update EMA with a new importance score."""
        current = await self.get_threshold()

        # Don't adapt too aggressively to extreme values
        clamped_score = max(self.MIN_THRESHOLD, min(self.MAX_THRESHOLD, new_score))

        # EMA Formula: T = alpha * score + (1 - alpha) * T
        updated = self.ALPHA * clamped_score + (1 - self.ALPHA) * current

        # Hard limits for safety
        updated = max(self.MIN_THRESHOLD, min(self.MAX_THRESHOLD, updated))

        self._current_value = updated
        await self._save(updated)

        # Update prometheus metric
        from shared.metrics import metrics

        metrics.current_importance_threshold.set(updated)

        return updated

    async def _save(self, value: float) -> None:
        """Persist threshold to DB."""
        try:
            conn = await connection_manager.get("memory.db")
            await conn.execute(
                """INSERT INTO preferences (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (self.key, str(value), time.time()),
            )
            await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to save adaptive threshold to DB: {e}")


# Singleton instance
adaptive_threshold = AdaptiveThresholdManager()
