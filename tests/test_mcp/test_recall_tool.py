"""D1.1: memory_recall_protocol registration + tier + recall CLI parsing."""


def test_registered_and_tier():
    from mcp_server.tools_layer import _register_tools

    assert "memory_recall_protocol" in _register_tools
    assert len(_register_tools) == 49  # 41 + recall + wiki_read + skill_promote + smart_context + reflect + scratchpad + quality + counterfactual
    # Operator tier: NOT primitive, NOT in any EXTRA_TIERS prefix match.
    from mcp_server.server import PRIMITIVE_TOOLS

    assert "memory_recall_protocol" not in PRIMITIVE_TOOLS


def test_recall_cli_parse():
    from autohooks import __main__ as cli

    ns = cli._parse_args(["recall", "--config", "x.yaml", "--query", "deploy", "--budget", "500"])
    assert ns.query == "deploy" and ns.budget == "500" and ns.format == "md"
    ns2 = cli._parse_args(["recall", "--config", "x.yaml"])
    assert ns2.query == "" and ns2.format == "md"
