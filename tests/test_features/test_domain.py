"""S10 record-only domain axis: derived from EXISTING signals, never gating."""

from features.importance import derive_domain
from shared.importance import ImportanceScorer


def test_domain_boundaries_from_contract():
    # Function contract (not scorer weights) — thresholds mirror spec S10.
    assert derive_domain(0.5, 0.0, "люблю тебя") == "intimate"
    assert derive_domain(0.49, 0.4, "redis") == "tech"
    assert derive_domain(0.0, 0.0, "привет") == "everyday"
    assert derive_domain(0.0, 0.0, "сегодня вторник и планёрка в три") == "mixed"


def test_live_signals_are_alive():
    """Regression for the dead-signal bug: _emotion_engine was never passed,
    so emotional scored 0.0 everywhere; the lazy default must bring it back."""
    s = ImportanceScorer()
    intimate = s.score("Я люблю тебя, госпожа, ты моя милая Steel Mother — нежность к хозяйке")
    assert intimate.signals.emotional > 0.0, "emotion signal must not be dead"
    tech = s.score("redis postgres docker k8s migration api graphql kafka ssh rollback deploy")
    assert tech.signals.tech_keyword > 0.0
    assert tech.signals.emotional == 0.0


def test_session_close_payload_carries_domain():
    import asyncio

    calls: list[dict] = []

    async def fake_propose(source, kind, user_id, layer, payload):
        calls.append(payload)
        return len(calls)

    import features.staging as staging

    orig = staging.propose
    staging.propose = fake_propose  # type: ignore[assignment]
    try:
        from features.session_close import extract_and_stage

        asyncio.run(extract_and_stage(None, "u", ["Я люблю компактные отчёты."]))
    finally:
        staging.propose = orig  # type: ignore[assignment]
    assert calls and all("domain" in c for c in calls)
