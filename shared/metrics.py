"""Metrics — Professional Prometheus-compatible metrics collection.
Uses prometheus_client for standard compliance and advanced histograms.
"""

import logging
import time
from typing import Any

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, generate_latest

logger = logging.getLogger(__name__)


class MetricsCollector:
    """Centralized metrics management using prometheus_client."""

    def __init__(self) -> None:
        self._start_time = time.time()
        self._dynamic_metrics: dict[str, Any] = {}

        # --- Standard Metrics ---
        self.uptime = Gauge("ariel_memory_uptime_seconds", "Server uptime in seconds")
        self.uptime.set_function(lambda: time.time() - self._start_time)

        # --- Domain Metrics ---
        self.memory_ops_total = Counter("ariel_memory_ops_total", "Total memory operations", ["action", "layer"])
        self.importance_filtered_total = Counter("ariel_memory_filtered_total", "Total messages filtered by importance", ["reason"])
        self.current_importance_threshold = Gauge("ariel_memory_importance_threshold", "Current EMA importance threshold")

        # --- Performance Metrics ---
        self.search_latency = Histogram(
            "ariel_memory_search_latency_seconds", "Latency of memory search operations", buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0)
        )

    def inc(self, name: str, value: float = 1) -> None:
        """Legacy compatibility wrapper."""
        # Map simple counter name to the labeled counter if possible
        if name == "importance_bypassed_total":
            self.importance_filtered_total.labels(reason="below_threshold").inc(value)
        else:
            # Create a generic counter if not matched, using cache to avoid DuplicateTimeseries
            m_name = f"ariel_memory_{name}"
            if m_name not in self._dynamic_metrics:
                self._dynamic_metrics[m_name] = Counter(m_name, f"Legacy counter: {name}")
            self._dynamic_metrics[m_name].inc(value)

    def gauge(self, name: str, value: float) -> None:
        """Legacy compatibility wrapper."""
        if name == "importance_threshold":
            self.current_importance_threshold.set(value)
        else:
            m_name = f"ariel_memory_{name}"
            if m_name not in self._dynamic_metrics:
                self._dynamic_metrics[m_name] = Gauge(m_name, f"Legacy gauge: {name}")
            self._dynamic_metrics[m_name].set(value)

    def render_prometheus(self) -> str:
        """Render all metrics in Prometheus format."""
        return generate_latest(REGISTRY).decode("utf-8")

    def render_json(self) -> dict[str, Any]:
        """Legacy compatibility: render minimal JSON for dashboard."""
        counters = {}
        for name, metric in self._dynamic_metrics.items():
            if isinstance(metric, Counter):
                # Strip prefix for legacy output
                clean_name = name.replace("ariel_memory_", "")
                counters[clean_name] = metric._value.get()

        return {"uptime_seconds": time.time() - self._start_time, "status": "ok (prometheus_client active)", "counters": counters}


# Global instance — used by server.py and middleware
metrics = MetricsCollector()
