---
type: "Reference"
title: "Testing and quality gates"
openwiki_generated: true
verified:
  - by: openwiki/0.6.0
    at: 2026-09-24T20:32:03.830Z
sources:
  - id: openwiki-source-4d1645cb6317345817452838
    resource: repo://.pre-commit-config.yaml
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-838f4da13ec2c12002f1cb15
    resource: repo://tests/test_code_policy.py
generated: { by: "opencode", at: "2026-09-24T20:32:03.830Z" }
---

# Testing and quality gates

Tests run in the repo venv (`./.venv/bin/python -m pytest`). The pre-push
gate is full-repo with no pipes (pipes mask exit codes): `ruff check .`,
then `ruff format --check .`, then mypy over the typed surface, then the
full pytest suite.

## Gate order

1. `ruff check .` — lint.
2. `ruff format --check .` — formatting.
3. mypy over `features/ shared/ mcp_server/ rag/ hooks/ wiki/ lifecycle/
   graph/ core/ autohooks/`.
4. `pytest tests/ -q` (asyncio, timeout, cov, and xdist plugins per
   `pyproject.toml`).

Check `df /tmp` before pushing — a full tmpfs kills the gate silently.

## Pre-commit

`.pre-commit-config.yaml` runs trailing-whitespace / end-of-file / yaml /
large-file checks, ruff + ruff-format, and a local skylos security hook
(with `.venv-embeddings` excluded as vendored code).

## Code policy

- Comments and docstrings in ENGLISH only (Cyrillic only as data),
  enforced by `tests/test_code_policy.py`.
- A `pyproject.toml` change must commit the regenerated `uv.lock` in the
  same commit.
- Every MCP server gets its own dedicated venv; the shared e5 embeddings
  service is long-lived and is not restarted casually.
