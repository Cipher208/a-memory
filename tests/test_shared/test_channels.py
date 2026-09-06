"""A4 channel granularity tests: canonical `channel[:detail]` source format."""

from shared.channels import KNOWN_CHANNELS, canonical_source, channel_of


def test_known_channels_set() -> None:
    assert {
        "user_explicit",
        "episode_promotion",
        "auto_save",
        "tool",
        "import",
        "shared",
        "consolidation",
        "branch_merge",
        "snapshot_restore",
    } <= KNOWN_CHANNELS


def test_channel_of_parses_and_defaults() -> None:
    assert channel_of("tool:mcp_remember") == "tool"
    assert channel_of("user_explicit") == "user_explicit"
    assert channel_of(None) == "other"
    assert channel_of("") == "other"
    assert channel_of("weird-legacy-source") == "other"


def test_canonical_source() -> None:
    assert canonical_source("tool", "mcp_remember") == "tool:mcp_remember"
    assert canonical_source("shared") == "shared"
