from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import Context

from mcp_server.models import ProjectResult
from mcp_server.registry import _get_ctx
from shared.metrics import metrics

from mcp_server.tools.base import _validate_layer, _get_memory, _get_wiki, _get_rag

# Runtime imports: MCPServer evaluates tool annotations at registration;
# hiding Context/AppContext under TYPE_CHECKING breaks tools/list (fix 419d577).
from mcp.server.mcpserver import Context  # noqa: TC002
from mcp_server.context import AppContext  # noqa: TC001

logger = logging.getLogger(__name__)


_Handler = Callable[..., Awaitable[tuple[str, dict[str, Any], str | None, str | None]]]


@dataclass
class _ProjectCtx:
    """Shared dependencies for one project() invocation."""

    app: AppContext
    layer: str
    user_id: str
    wiki: Any
    mem: Any
    pm: Any


async def project(
    action: Literal["init", "update", "archive", "mapping", "audit", "decision", "recall"],
    name: str,
    details: str = "",
    role: str = "",
    status: str = "",
    path: str = "",
    decision: str = "",
    outcome: str = "",
    layer: Literal["user", "agent"] = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Universal Primitive: managing project-specific context and file mapping.

    Projects are global (keyed by name). Structured data (identity,
    decisions, artifact map, code index) lives in projects.db; large
    documents go to the Wiki as project_spec pages.
    """
    app: AppContext = _get_ctx(ctx)
    metrics.inc("tool_calls")
    metrics.inc("tool_project")

    _validate_layer(layer)

    from core.projects import ProjectMemory

    pm = ProjectMemory(cm=app.mm._cm)
    await pm._init_db()

    pctx = _ProjectCtx(
        app=app,
        layer=layer,
        user_id=user_id,
        wiki=_get_wiki(app, layer),
        mem=_get_memory(app, layer, user_id),
        pm=pm,
    )

    handlers: dict[str, _Handler] = {
        "init": _action_init,
        "update": _action_update,
        "archive": _action_archive,
        "mapping": _action_mapping,
        "audit": _action_audit,
        "decision": _action_decision,
        "recall": _action_recall,
    }
    handler = handlers.get(action)
    if handler is None:
        return ProjectResult(status="error", title=name, audit_report=f"unknown action: {action}").dict()

    status_res, result_extra, audit_report, wiki_path = await handler(
        pctx,
        name=name,
        user_id=user_id,
        details=details,
        role=role,
        status=status,
        path=path,
        decision=decision,
        outcome=outcome,
    )
    return {**ProjectResult(status=status_res, title=name, path=wiki_path, audit_report=audit_report).dict(), **result_extra}


async def _action_init(pctx: _ProjectCtx, *, name: str, details: str, path: str, **_: Any) -> tuple[str, dict[str, Any], None, str | None]:
    wiki_path = await pctx.wiki.add(wiki_type="project_spec", title=name, content=details)
    await pctx.pm.upsert_project(name, summary=details[:500], path=path)
    return "ok", {"wiki_ref": wiki_path}, None, wiki_path


async def _action_update(
    pctx: _ProjectCtx, *, name: str, details: str, status: str, path: str, **_: Any
) -> tuple[str, dict[str, Any], None, str | None]:
    results = await pctx.wiki.index.search(name, limit=1)
    if results and results[0]["title"] == name:
        await pctx.wiki.update(file_path=results[0]["file_path"], content=details)
        wiki_path: str | None = results[0]["file_path"]
        status_res = "updated"
    else:
        wiki_path = await pctx.wiki.add(wiki_type="project_spec", title=name, content=details)
        status_res = "ok"

    existing = await pctx.pm.get_project(name)
    fs_path = path or str((existing or {}).get("path") or "")
    await pctx.pm.upsert_project(name, summary=details[:500], status=status, path=fs_path)
    code_map = await _refresh_code_map(pctx.pm, name, fs_path)
    return status_res, {"code_map": code_map}, None, wiki_path


async def _action_mapping(
    pctx: _ProjectCtx, *, name: str, details: str, role: str, status: str, **_: Any
) -> tuple[str, dict[str, Any], None, str | None]:
    # name = PROJECT, details = artifact path. Registers the file in the
    # project map (SQLite) with role/status, plus a Wiki page for notes.
    mapping_content = f"File: {details} | Role: {role} | Status: {status}"
    wiki_path = await pctx.wiki.add(wiki_type="project_spec", title=f"Map_{details or name}", content=mapping_content)
    await pctx.pm.upsert_artifact(name, path=details or name, role=role, status=status, wiki_ref=wiki_path)
    return "mapped", {}, None, wiki_path


async def _action_decision(
    pctx: _ProjectCtx, *, name: str, details: str, decision: str, outcome: str, **_: Any
) -> tuple[str, dict[str, Any], str | None, None]:
    if not decision:
        return "error", {}, "decision text required", None
    await pctx.pm.add_decision(name, decision=decision, rationale=details, outcome=outcome)
    if pctx.app.temporal:
        with contextlib.suppress(Exception):
            await pctx.app.temporal.add_event(
                pctx.user_id,
                "project_decision",
                f"{name}: {decision}"[:200],
                importance=0.8,
                metadata={"outcome": outcome[:100]},
                layer=pctx.layer,
            )
    return "decided", {}, None, None


async def _action_recall(pctx: _ProjectCtx, *, name: str, **_: Any) -> tuple[str, dict[str, Any], str | None, None]:
    proj = await pctx.pm.get_project(name)
    decisions = await pctx.pm.list_decisions(name)
    artifacts = await pctx.pm.list_artifacts(name)
    symbols_n = await pctx.pm.count_symbols(name)
    audit_report = "\n".join(
        [
            f"Project: {name}",
            f"Status: {(proj or {}).get('status', 'unknown')} | Summary: {(proj or {}).get('summary', '—')[:200]}",
            f"Decisions ({len(decisions)}):",
            *[f"  - [{d['created_at']:.0f}] {d['decision'][:100]} → {d['outcome'][:80]}" for d in decisions[:10]],
            f"Artifacts: {len(artifacts)} tracked",
            f"Code index: {symbols_n} symbols",
        ]
    )
    extra = {
        "project": proj,
        "decisions": decisions,
        "artifacts": artifacts,
        "code_symbols": symbols_n,
    }
    return "recalled", extra, audit_report, None


async def _action_archive(pctx: _ProjectCtx, *, name: str, details: str, user_id: str, **_: Any) -> tuple[str, dict[str, Any], None, None]:
    results = await pctx.wiki.index.search(name, limit=1)
    if not (results and results[0]["title"] == name):
        return "not_found", {}, None, None

    from shared.archived_memories import ArchivedMemories

    am = ArchivedMemories(cm=pctx.wiki._cm)
    await am.archive(
        user_id=user_id,
        content=details or results[0].get("content", ""),
        memory_type="project_archive",
        importance=0.8,
        original_id=0,
        reason="project_archived",
    )
    await pctx.wiki.delete(results[0]["file_path"])
    return "archived", {}, None, None


async def _action_audit(pctx: _ProjectCtx, *, name: str, user_id: str, **_: Any) -> tuple[str, dict[str, Any], str | None, None]:
    """Dream-based gap analysis + project-store completeness."""
    multi_rag = _get_rag(pctx.app, pctx.layer)
    verdicts: list[str] = []

    # 1. Targeted searches per dimension (dream-style, not substring scans)
    dimensions = {
        "Architecture/Design": f"{name} architecture design schema",
        "Security/Hardening": f"{name} security hardening secrets",
        "Testing/Verification": f"{name} testing verification coverage",
    }
    for label, query in dimensions.items():
        hits = await multi_rag.search(query, user_id=user_id, limit=5, intent="balanced")
        if hits:
            verdicts.append(f"{label}: {len(hits)} related entries found.")
        else:
            verdicts.append(f"{label} documentation is missing.")

    # 2. Conflict Detection in L4 (CoreMemory)
    l4_hits = await pctx.mem.l4.search(user_id, name, limit=50)
    keys_seen: dict[str, Any] = {}
    for hit in l4_hits:
        key = hit.get("key")
        val = hit.get("value")
        if key in keys_seen and keys_seen[key] != val:
            verdicts.append(f"Memory conflict: '{key}' has multiple values ('{keys_seen[key]}' vs '{val}')")
        keys_seen[key] = val
    if not any(v.startswith("Memory conflict") for v in verdicts):
        verdicts.append("No memory conflicts detected in CoreMemory.")

    # 3. Project store completeness (projects.db)
    proj = await pctx.pm.get_project(name)
    decisions = await pctx.pm.list_decisions(name)
    artifacts = await pctx.pm.list_artifacts(name)
    if not proj:
        verdicts.append("Project identity is missing from projects.db (run init).")
    elif proj.get("summary"):
        verdicts.append("Project summary is present.")
    else:
        verdicts.append("Project summary is empty.")
    if decisions:
        latest = decisions[0]
        age_days = max(0.0, (time.time() - float(latest.get("created_at", time.time()))) / 86400)
        verdicts.append(f"Decisions recorded: {len(decisions)} (latest {age_days:.0f}d ago).")
    else:
        verdicts.append("No decisions recorded — outcome history is missing.")
    verdicts.append(f"Artifact map: {len(artifacts)} tracked files.")

    extra = {"decisions": decisions, "artifacts": artifacts}
    return "audit", extra, "\n".join(verdicts), None


async def _refresh_code_map(pm: Any, project_name: str, project_path: str) -> str:
    """Refresh the code-symbol index via graphify.

    Optional capability: if the binary is absent or the path is not a
    code project, skip with a note.
    """
    if not project_path or not Path(project_path).is_dir():  # noqa: ASYNC240 — stat-only check
        return "skipped: no local path"
    import shutil

    if shutil.which("graphify") is None:
        return "skipped: graphify not installed"

    graph_file = Path(project_path) / "graphify-out" / "graph.json"
    try:
        cmd = ["graphify", "update", project_path, "--no-viz"] if graph_file.exists() else ["graphify", "extract", project_path]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=project_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await asyncio.wait_for(proc.wait(), timeout=600)
        if proc.returncode != 0 or not graph_file.exists():
            return f"skipped: graphify exit {proc.returncode}"

        graph = json.loads(graph_file.read_text(encoding="utf-8"))
        indexed = await pm.replace_symbols(project_name, graph.get("nodes", []))
        return f"ok: {indexed} symbols"
    except Exception as e:
        return f"skipped: {type(e).__name__}: {e}"
