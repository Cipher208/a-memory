"""D3.5: dispatch --payload + inject --blocks CLI behavior."""

import pytest

from autohooks import __main__ as cli
from autohooks.config import AgentConfig, FieldMap, SourceConfig


def _cfg(tmp_path):
    src = SourceConfig(
        driver="sqlite",
        path=tmp_path / "conv.db",
        table="t",
        cursor_column="rowid",
        order_by="rowid",
        role=FieldMap(column="role"),
        text=FieldMap(column="text"),
    )
    return AgentConfig(data_dir=tmp_path, user_id="default", layer="user", source=src)


def test_parse_args_payload_and_blocks():
    ns = cli._parse_args(
        ["dispatch", "--config", "x.yaml", "--event", "post_context_compression", "--payload", '{"a":1}']
    )
    assert ns.payload == '{"a":1}'
    ns2 = cli._parse_args(["inject", "--config", "x.yaml", "--blocks", "rehydrate"])
    assert ns2.blocks == "rehydrate"
    ns3 = cli._parse_args(["dispatch", "--config", "x.yaml", "--event", "new_message"])
    assert ns3.payload == "{}"


@pytest.mark.asyncio
async def test_run_inject_blocks_filter(tmp_path):
    async def fake_dispatch(event, layer, user_id, payload, mem, graph, rag):
        return {
            "results": [
                {
                    "blocks": [
                        {"kind": "important", "content": "fact", "score": 0.9},
                        {"kind": "rehydrate", "content": "reh", "score": 0.9},
                    ]
                }
            ]
        }

    from autohooks.inject import run_inject

    out = await run_inject(_cfg(tmp_path), None, None, None, dispatch=fake_dispatch, blocks="rehydrate")
    assert "reh" in out and "fact" not in out
    out_all = await run_inject(_cfg(tmp_path), None, None, None, dispatch=fake_dispatch)
    assert "fact" in out_all and "reh" in out_all


def test_main_dispatch_invalid_payload_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "autohooks",
            "dispatch",
            "--config",
            str(tmp_path / "missing.yaml"),
            "--event",
            "post_context_compression",
            "--payload",
            "not-json",
        ],
    )
    assert cli.main() == 2
