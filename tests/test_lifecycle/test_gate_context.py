"""K2: kind-aware thresholds; preference at 0.6 must reach L4 review.

Spec S3 (2026-09-13): session_close preferences (0.6) died against the flat
0.7 fact gate — 225 expired proposals in the hermes base.
"""

import asyncio

from lifecycle.consolidation import passes_promotion_gate


def test_observation_at_065_still_gated():
    item = {"content": "оказалось что хуки вызывают CLI, а не MCP-тул recall напрямую", "importance": 0.65, "memory_kind": "observation"}
    assert not passes_promotion_gate(item, 0.7)


def test_preference_at_06_passes():
    item = {"content": "Лили любит чтобы рестарты сервисов делались после зелёного гейта", "importance": 0.6, "memory_kind": "preference"}
    assert passes_promotion_gate(item, 0.7)


def test_rule_at_06_passes():
    item = {"content": "больше не делать пуш без df /tmp перед полным гейтом", "importance": 0.6, "memory_kind": "rule"}
    assert passes_promotion_gate(item, 0.7)


def test_broadcast_still_killed_at_any_kind():
    content = "Статус: консоль запущена, все сервисы активны, память свободна"
    from shared.broadcast import is_status_broadcast

    if is_status_broadcast(content):
        item = {"content": content, "importance": 0.9, "memory_kind": "preference"}
        assert not passes_promotion_gate(item, 0.7)


def test_session_close_payloads_carry_memory_kind():
    calls: list[dict] = []

    async def fake_propose(source, kind, user_id, layer, payload):
        calls.append(payload)
        return len(calls)

    import features.staging as staging

    orig = staging.propose
    staging.propose = fake_propose  # type: ignore[assignment]
    try:
        from features.session_close import extract_and_stage

        asyncio.run(extract_and_stage(None, "u", ["Я люблю компактные отчёты. Оказалось что это работает только после рестарта."]))
    finally:
        staging.propose = orig  # type: ignore[assignment]
    assert calls, "pattern extraction produced nothing"
    kinds = {c["memory_kind"] for c in calls}
    assert "preference" in kinds
    assert all(c["importance"] >= 0.5 for c in calls)
