"""D1.5 verify_hits — query-overlap verification of retrieved hits."""

from features.verify import content_tokens, verify_hits


def test_verified_by_overlap():
    hits = [
        {"content": "deploy pipeline uses ariel venv uv sync", "score": 0.9, "source": "fts"},
        {"content": "user likes pineapple pizza", "score": 0.8, "source": "fts"},
    ]
    verified, dropped = verify_hits("how does the deploy pipeline work", hits)
    assert [h["content"] for h in verified] == [hits[0]["content"]]
    assert dropped == [hits[1]]


def test_expand_hits_not_special_cased_here():
    # verify_hits is source-agnostic; recall exempts expand axis by partition.
    hits = [{"content": "graph neighbor of verified hit", "score": 0.4, "source": "graph_expand"}]
    verified, dropped = verify_hits("deploy pipeline", hits)
    assert verified == [] and dropped == hits


def test_empty_query_verifies_everything():
    hits = [{"content": "anything at all", "score": 0.5}]
    verified, dropped = verify_hits("", hits)
    assert verified == hits and dropped == []


def test_stopwords_dont_count_as_overlap():
    assert content_tokens("what was this about") == set()
