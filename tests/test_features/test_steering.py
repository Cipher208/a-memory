"""D1.3 steering_hints — route table + keyword intent match."""


def test_empty_query_returns_full_table():
    from features.steering import ROUTE_TABLE, steering_hints

    hints = steering_hints("")
    assert len(hints) == len(ROUTE_TABLE) == 8
    assert all({"when", "use", "instead", "why"} <= set(h) for h in hints)


def test_ru_intent_matches_recall_route():
    from features.steering import steering_hints

    hints = steering_hints("вспомни что мы говорили о деплое")
    assert hints, "no hints matched"
    assert "memory_recall_protocol" in hints[0]["use"]


def test_en_intent_matches_wiki_route():
    from features.steering import steering_hints

    hints = steering_hints("how do i run the migration procedure")
    assert hints, "no hints matched"
    assert "wiki_search" in hints[0]["use"]


def test_hints_capped_at_three():
    from features.steering import steering_hints

    hints = steering_hints("вспомни процедуру skill и proposal статистик")
    assert len(hints) == 3
