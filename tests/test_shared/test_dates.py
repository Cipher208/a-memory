"""S4: shared UTC date stamp for L4 rows entering LLM context."""

from types import SimpleNamespace

from shared.dates import row_stamp, utc_date_prefix


def test_utc_date_prefix_formats_posix_ts() -> None:
    assert utc_date_prefix(1788880035.0) == "2026-09-08 "


def test_utc_date_prefix_none_garbage_and_str() -> None:
    assert utc_date_prefix(None) == ""
    assert utc_date_prefix("not-a-number") == ""
    assert utc_date_prefix("1788880035") == "2026-09-08 "


def test_row_stamp_prefers_updated_then_created_then_empty() -> None:
    assert row_stamp(SimpleNamespace(updated_at=1788880035.0, created_at=0.0)) == "2026-09-08 "
    assert row_stamp(SimpleNamespace(updated_at=None, created_at=1788880035.0)) == "2026-09-08 "
    assert row_stamp(SimpleNamespace()) == ""
