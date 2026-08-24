import pytest

from shared.metrics import metrics


@pytest.mark.asyncio
async def test_metrics_registry():
    """Verify that our custom metrics are in the Prometheus registry."""
    # Trigger some metrics
    metrics.memory_ops_total.labels(action="test_op", layer="test_layer").inc()
    metrics.current_importance_threshold.set(0.42)

    # Render and check
    output = metrics.render_prometheus()
    assert "ariel_memory_ops_total" in output
    assert 'action="test_op"' in output
    assert "ariel_memory_importance_threshold 0.42" in output
