"""D1.11 — memory branches: layer-namespace A/B staging for L4 facts."""

import pytest

from core.memory import CoreMemory
from features import branches as br
from shared.connection import AsyncConnectionManager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path):
    mgr = AsyncConnectionManager(base_dir=str(tmp_path))
    await MigrationManager(cm=mgr).migrate()
    return mgr


async def _seed_main(cm, user_id="u1"):
    core = CoreMemory(cm=cm, layer="user")
    await core.save(user_id, "principle_yagni", "YAGNI ruthlessly", importance=0.9)
    await core.save(user_id, "principle_kiss", "Keep it simple", importance=0.8)


def test_validate_branch_name():
    assert br.validate_branch_name("exp1") == "exp1"
    assert br.validate_branch_name("ab-test_2") == "ab-test_2"
    for bad in ("", "UPPER", "has@at", "x" * 33, "-lead"):
        with pytest.raises(ValueError):
            br.validate_branch_name(bad)
    with pytest.raises(ValueError):
        br.branch_layer("graph", "exp1")  # base layer must be user|agent


async def test_create_clone_isolation_and_branch_write(cm):
    await _seed_main(cm)

    created = await br.create_branch(cm, "user", "u1", "exp1")
    assert created["copied"] == 2

    # branch diverges: change one, add one
    await br.write_branch(cm, "user", "u1", "exp1", "principle_yagni", "BUT: test first", importance=0.7)
    await br.write_branch(cm, "user", "u1", "exp1", "principle_ab", "A/B test personas")

    # main untouched
    main = CoreMemory(cm=cm, layer="user")
    assert (await main.get("u1", "principle_yagni")).value == "YAGNI ruthlessly"
    assert await main.get("u1", "principle_ab") is None

    # branch holds both
    facts = await br.read_branch(cm, "user", "u1", "exp1")
    assert {f["key"] for f in facts} == {"principle_yagni", "principle_kiss", "principle_ab"}


async def test_diff_and_cherry_pick_merge(cm):
    await _seed_main(cm)
    await br.create_branch(cm, "user", "u1", "exp1")
    await br.write_branch(cm, "user", "u1", "exp1", "principle_yagni", "BUT: test first", importance=0.7)
    await br.write_branch(cm, "user", "u1", "exp1", "principle_ab", "A/B test personas")

    diff = await br.diff_branch(cm, "user", "u1", "exp1")
    assert diff["added"] == ["principle_ab"] and diff["changed"] == ["principle_yagni"]
    assert diff["unchanged"] == ["principle_kiss"]

    # cherry-pick: ONLY the added principle lands on main
    merged = await br.merge_branch(cm, "user", "u1", "exp1", keys=["principle_ab"])
    assert merged["merged"] == ["principle_ab"] and merged["skipped"] == []

    main = CoreMemory(cm=cm, layer="user")
    assert (await main.get("u1", "principle_ab")).value == "A/B test personas"
    assert (await main.get("u1", "principle_yagni")).value == "YAGNI ruthlessly"

    # merge provenance in the A2.2 ledger
    from features.history import list_history

    hist = await list_history(cm, "u1", "user", key="principle_ab")
    assert hist[0]["triggered_by"] == "branch_merge:exp1"

    # default merge-all covers the remaining difference
    merged_all = await br.merge_branch(cm, "user", "u1", "exp1")
    assert merged_all["merged"] == ["principle_yagni"]
    assert (await main.get("u1", "principle_yagni")).value == "BUT: test first"


async def test_merge_skips_unchanged_and_unknown_keys(cm):
    await _seed_main(cm)
    await br.create_branch(cm, "user", "u1", "exp1")
    out = await br.merge_branch(cm, "user", "u1", "exp1", keys=["principle_kiss", "ghost_key"])
    assert out["merged"] == [] and sorted(out["skipped"]) == ["ghost_key", "principle_kiss"]


async def test_delete_and_list_branches(cm):
    await _seed_main(cm)
    await br.create_branch(cm, "user", "u1", "exp1")
    await br.create_branch(cm, "user", "u1", "exp2")

    listed = await br.list_branches(cm, user_id="u1")
    assert [b["name"] for b in listed] == ["exp1", "exp2"]
    assert listed[0]["base_layer"] == "user" and listed[0]["facts"] == 2

    deleted = await br.delete_branch(cm, "user", "u1", "exp1")
    assert deleted["deleted"] == 2
    assert [b["name"] for b in await br.list_branches(cm, user_id="u1")] == ["exp2"]

    # deleting an absent but VALID branch is a no-op report, not an error
    deleted = await br.delete_branch(cm, "user", "u1", "ghost")  # valid name, absent rows
    assert deleted["deleted"] == 0
