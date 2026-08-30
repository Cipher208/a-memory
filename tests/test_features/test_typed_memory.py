"""D1.8 typed memory schemas — validation + custom YAML schemas."""

import pytest


class _FakeL4:
    def __init__(self):
        self.saved = []

    async def save(self, user_id, key, value, importance=None, memory_kind=None, expires_at=None, source="manual", metadata=None, layer=None):
        self.saved.append({"user_id": user_id, "key": key, "value": value, "metadata": metadata, "source": source})
        return len(self.saved)


class _FakeMem:
    def __init__(self):
        self.l4 = _FakeL4()


@pytest.mark.asyncio
async def test_valid_decision_saves_as_typed_fact():
    from features.typed_memory import save_typed

    mem = _FakeMem()
    res = await save_typed(mem, "u1", "decision", {"decision": "ship ariel 1.9", "rationale": "707 tests green"})
    assert res["key"] == "decision:ship ariel 1.9"
    saved = mem.l4.saved[0]
    assert saved["metadata"] == {"typed": "decision"}
    assert "decision: ship ariel 1.9" in saved["value"]


@pytest.mark.asyncio
async def test_missing_required_field_raises():
    from features.typed_memory import save_typed

    with pytest.raises(ValueError, match="missing required field: error"):
        await save_typed(_FakeMem(), "u1", "error_pattern", {"cause": "unclear"})


@pytest.mark.asyncio
async def test_unknown_type_lists_available():
    from features.typed_memory import save_typed

    with pytest.raises(ValueError, match="decision"):
        await save_typed(_FakeMem(), "u1", "nope", {"x": "y"})


@pytest.mark.asyncio
async def test_unknown_field_rejected():
    from features.typed_memory import save_typed

    with pytest.raises(ValueError, match="unknown fields"):
        await save_typed(_FakeMem(), "u1", "relationship", {"name": "bob", "wibble": "1"})


@pytest.mark.asyncio
async def test_custom_yaml_schema(tmp_path, monkeypatch):
    from shared.connection import connection_manager

    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    try:
        schemas = tmp_path / "schemas"
        schemas.mkdir()
        (schemas / "habit.yaml").write_text("habit:\n  name: {required: true, max_len: 100}\n  cadence: str\n", encoding="utf-8")
        from features.typed_memory import available_schemas, save_typed

        assert "habit" in available_schemas()
        res = await save_typed(_FakeMem(), "u1", "habit", {"name": "morning run", "cadence": "daily"})
        assert res["key"] == "habit:morning run"
    finally:
        connection_manager.base_dir = original
