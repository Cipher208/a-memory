"""Code policy: hot files must have English-only comments and docstrings.

RU string literals (classification keywords, synonym maps, morphology
patterns, regex token sets) are product data and stay — only comments and
docstrings are regulated by the "comments in English" rule.
"""

import ast
import io
import tokenize
from pathlib import Path

import pytest

# The database write path. These modules decide who holds the lock, who is
# allowed to write, and which database failure is worth retrying, so their
# comments and docstrings are read by operators debugging a live base -- they
# must be covered by the policy like any other hot file.
#
# The list FEEDS the policy list below instead of being checked against it.
# The old version asserted that one hand-maintained list was a subset of
# another hand-maintained list in the same file: it passed by construction,
# it discovered nothing, and it would have kept passing after either list had
# been edited wrongly. Folding the list into the policy makes the guarantee
# structural -- there is nothing left to assert, and a file added here is
# covered the moment it is added.
REQUIRED_WRITE_PATH_FILES = [
    "shared/lock_owners.py",
    "shared/write_dispatch.py",
    "shared/sqlite_busy.py",
]

HOT_FILES = [
    "lifecycle/graph_miners.py",
    "rag/edm.py",
    "lifecycle/distiller.py",
    "rag/dual_route.py",
    "rag/ablation.py",
    "lifecycle/graph_enrich.py",
    "lifecycle/graph_sanitation.py",
    "mcp_server/utils/privacy.py",
    "mcp_server/annotations.py",
    "core/memory.py",
    "lifecycle/l0_tiers.py",
    "lifecycle/segment_consolidation.py",
    "autohooks/context.py",
    "autohooks/__main__.py",
    "autohooks/daemon.py",
    "hooks/registry.py",
    "shared/l0.py",
    "features/wiki_reconciliation.py",
    "features/offload.py",
    "rag/multi_source.py",
    "features/diagnostics.py",
    "lifecycle/qfields.py",
    "shared/uris.py",
    "mcp_server/tools/ops.py",
    "lifecycle/embedding_clusters.py",
    "features/decision_trace.py",
    "shared/morph.py",
    "shared/fractional_index.py",
    "hooks/external.py",
    "features/session_close.py",
    "features/replay.py",
    "features/inject.py",
    "features/bridge.py",
    "features/backup_cron.py",
    "lifecycle/gap_registry.py",
    "lifecycle/consolidation.py",
    "shared/textcat.py",
    "lifecycle/tool_stats.py",
    "features/cycles.py",
    "rag/synonyms.py",
    "mcp_server/tools/primitives/think.py",
    "mcp_server/tools/memory.py",
    "hooks/agent_hooks.py",
    "features/wake_up.py",
    "features/recall.py",
    "features/l2_enrich.py",
    "rag/quantize.py",
    "mcp_server/tools/wiki.py",
    "lifecycle/l0_sweep.py",
    "hooks/user_hooks.py",
    "shared/textcat_data.py",
    "rag/search.py",
    "mcp_server/tools/graph.py",
    "mcp_server/server.py",
    "lifecycle/graph_mermaid.py",
    "lifecycle/graph_builder.py",
    "shared/memory_types.py",
    "shared/importance/signals/tech_signal.py",
    *REQUIRED_WRITE_PATH_FILES,
]

ROOT = Path(__file__).resolve().parents[1]


def _has_cyrillic(text: str) -> bool:
    return any("\u0400" <= ch <= "\u04ff" for ch in text)


@pytest.mark.parametrize("relpath", HOT_FILES)
def test_comments_are_english(relpath: str) -> None:
    src = (ROOT / relpath).read_text(encoding="utf-8")
    offenders = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT and _has_cyrillic(tok.string):
            offenders.append(f"line {tok.start[0]}: {tok.string[:70]}")
    assert not offenders, f"{relpath} RU comments:\n" + "\n".join(offenders)


@pytest.mark.parametrize("relpath", HOT_FILES)
def test_docstrings_are_english(relpath: str) -> None:
    src = (ROOT / relpath).read_text(encoding="utf-8")
    offenders = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc and _has_cyrillic(doc):
                name = getattr(node, "name", "<module>")
                first = doc.strip().splitlines()[0][:70]
                offenders.append(f"{name}: {first}")
    assert not offenders, f"{relpath} RU docstrings:\n" + "\n".join(offenders)
