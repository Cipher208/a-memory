"""D1.9 rules engine — YAML rules, apply_rules, auto_save_text integration."""

import sqlite3

import pytest

from shared.connection import connection_manager

RULES_YAML = """
rules:
  - name: release-facts
    when_content_contains: ["release", "релиз"]
    importance_boost: 0.1
    tags: ["release"]
  - name: architecture
    when_content_contains: ["architecture", "архитектур"]
    importance_boost: 0.2
"""


@pytest.fixture
async def rules_dir(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    await connection_manager.close_all()
    yield tmp_path
    connection_manager.base_dir = original
    await connection_manager.close_all()


def _write_rules(d, content=RULES_YAML):
    (d / "rules.yaml").write_text(content, encoding="utf-8")


def test_missing_file_is_empty_ruleset(rules_dir):
    from features.rules import apply_rules, load_rules

    assert load_rules() == []
    out = apply_rules("some release happened")
    assert out == {"importance_boost": 0.0, "tags": [], "matched": []}


def test_rules_match_boost_and_tags(rules_dir):
    import features.rules as rules_mod

    _write_rules(rules_dir)
    rules_mod.load_rules(force=True)
    out = rules_mod.apply_rules("We shipped the RELEASE today")
    assert out["importance_boost"] == 0.1
    assert out["tags"] == ["release"]
    assert out["matched"] == ["release-facts"]
    assert rules_mod.apply_rules("nothing relevant here")["matched"] == []


def test_boost_sum_capped(rules_dir):
    import features.rules as rules_mod

    _write_rules(rules_dir, RULES_YAML + '  - name: rel2\n    when_content_contains: ["release"]\n    importance_boost: 0.5\n')
    rules_mod.load_rules(force=True)
    out = rules_mod.apply_rules("the release")
    assert out["importance_boost"] == 0.3  # 0.1 + 0.5 → cap
    assert len(out["matched"]) == 2


def test_mtime_cache_invalidates_only_on_force(rules_dir):
    import features.rules as rules_mod

    _write_rules(rules_dir)
    assert len(rules_mod.load_rules()) == 2
    _write_rules(rules_dir, "rules: []\n")
    # st_mtime has sub-second resolution here, so an unforced reload may
    # already see the change; force is the guaranteed invalidation path.
    assert len(rules_mod.load_rules(force=True)) == 0


class _FakeL3:
    def __init__(self):
        self.saved = []

    async def save(self, user_id, summary, weight, tags):
        self.saved.append((summary, weight, tags))
        return 1


class _FakeGraph:
    async def add_node(self, *a):
        return 1


class _FakeMem:
    def __init__(self):
        self.l3 = _FakeL3()
        self.l4 = type("L4", (), {"remember": staticmethod(lambda *a, **k: 1)})()

    async def remember(self, key, value, importance, source="user_explicit"):
        return 1


@pytest.mark.asyncio
async def test_auto_save_text_applies_rules(rules_dir):
    from hooks.external import auto_save_text

    _write_rules(rules_dir)
    conn = sqlite3.connect(str(rules_dir / "memory.db"))
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS memory_dispatch_log ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, source_msg_id INTEGER,"
        " layer TEXT NOT NULL DEFAULT 'user', user_id TEXT NOT NULL DEFAULT 'default', score REAL,"
        " saved_l3 INTEGER NOT NULL DEFAULT 0, saved_l4 INTEGER NOT NULL DEFAULT 0,"
        " saved_graph INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);"
        "CREATE TABLE IF NOT EXISTS mutation_proposals ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, kind TEXT NOT NULL,"
        " user_id TEXT NOT NULL DEFAULT 'default', layer TEXT NOT NULL DEFAULT 'user', payload TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending', proposed_at REAL NOT NULL, expires_at REAL NOT NULL,"
        " decided_at REAL, decided_by TEXT, result_ref TEXT);"
    )
    conn.commit()
    conn.close()

    mem = _FakeMem()
    # base score ~0.9 (len, ?, !, keywords, newlines) + 0.2 boost → saves
    text = "Изменил архитектуру памяти?\nТеперь это поэтапный план, который надо сделать!\nСначала прототип, потом тесты, иначе всё сломается — ты же помнишь, как я решил?"
    res = await auto_save_text(mem, _FakeGraph(), user_id="u1", text=text, event="new_message")
    assert res["rules"] == ["architecture"]
    assert res["score"] >= 0.5  # D1.9: importance_boost применился к гейту
    # F-G1: «как я решил» типизируется как decision → инвариант → L4, не L3
    assert res["routes"]["l4_saved"] == 1 and res["routes"]["l3_saved"] == 0
    assert mem.l3.saved == []
    # release rule adds its tag:
    res2 = await auto_save_text(
        mem,
        _FakeGraph(),
        user_id="u1",
        text="Релиз a-memory выйдет завтра?\nЭто важно: надо предупредить команду, обновить changelog и проверить CI перед публикацией сегодня!",
        event="new_message",
    )
    assert res2["rules"] == ["release-facts"]
    # F-G1: атомы → L3 с тегами [rule_tags..., event, kind] — rule-теги сохранены
    _, _, tags2 = mem.l3.saved[-1]
    assert "release" in tags2 and "new_message" in tags2
