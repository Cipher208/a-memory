"""A3.1: synonyms expansion — known terms OR-expand the FTS MATCH, others untouched."""

from rag.synonyms import expand_fts_query, load_synonyms


def test_builtin_table():
    table = load_synonyms()
    assert table["postgres"] == ["postgresql", "psql"]
    assert table["деплой"] == ["deploy", "deployment"]


def test_expansion_produces_or_group():
    out = expand_fts_query("postgres tuning")
    assert out == "(postgres OR postgresql OR psql) tuning"


def test_untouched_query_passthrough():
    assert expand_fts_query("plain query words") == "plain query words"
    assert expand_fts_query("") == ""


def test_punctuation_stripped_for_lookup():
    out = expand_fts_query("postgres, tuning")
    assert "(postgres OR postgresql OR psql)" in out


def test_case_insensitive_lookup_normalizes_token():
    """Lookup is case-insensitive; the group uses the normalized key (FTS is
    case-insensitive by default, so lowercase is the canonical form)."""
    out = expand_fts_query("Postgres tuning")
    assert out == "(postgres OR postgresql OR psql) tuning"


def test_config_override_merges(monkeypatch):
    """rag.synonyms config merges over built-ins (load_synonyms contract)."""
    import rag.synonyms as syn

    class _FakeConfig:
        def get(self, section, key, default=None):
            return {"redis": ["valkey"]}

    import config as config_mod

    monkeypatch.setattr(config_mod, "config", _FakeConfig())
    table = syn.load_synonyms()
    assert table["redis"] == ["valkey"]
    assert table["postgres"] == ["postgresql", "psql"]  # built-ins survive
    assert "valkey" in expand_fts_query("redis cluster", synonyms=table)
