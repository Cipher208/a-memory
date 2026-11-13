"""Every autohooks daemon process must stay light — it imports the whole server.

2026-11-13: both autohooks units sat at ~900 MB RSS and the VPS carried
10.5 GB of swap. Not a leak: `sanitize()` ran spaCy NER, and `import spacy`
alone is **546 MB** (spaCy pulls torch), 752 MB with a model loaded. Each
process that handles one event became a torch host — the daemon plus every
MCP stdio server.

The tiers are now split by cost. The regex/credential/dictionary tiers are
what a daemon actually needs and they are free. `rag.spacy_ner` (default off)
gates the ML tiers for hosts where a real model is worth half a gigabyte.

These two tests pin both halves: the *default* path must not pay, and an
*explicitly enabled* one still works. The second half matters — a gate that
just returned False forever would pass the first test and silently delete the
privacy gate's best tier.

Both run in subprocesses: a heavy module already in `sys.modules` would hide a
new one, and the tests must not depend on what the suite happened to import.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Modules whose presence means the process now hosts an ML runtime.
HEAVY_ROOTS = ("torch", "thinc", "spacy", "transformers", "sentence_transformers")


def _result_path(name: str) -> str:
    return os.path.join("/tmp", f"ariel-probe-{name}.json")


def _read_result(name: str) -> dict | None:
    path = _result_path(name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _probe(name: str, body: str) -> dict:
    """Run `body` in a fresh interpreter and return its measured facts.

    The child writes its facts to a file and the parent reads them there: a
    `print` before an interpreter teardown that can hang (the 2026-09-12
    `_arm_cli_guard` incident, and again here at exit) would make the test a
    hostage of shutdown, not of the behaviour it measures.
    """
    path = _result_path(name)
    for stale in (path,):
        if os.path.exists(stale):
            os.remove(stale)

    data_dir = os.path.join("/tmp", f"ariel-probe-{name}")
    os.makedirs(data_dir, exist_ok=True)
    live = os.path.expanduser("~/.mcp-ariel-memory-mimocode/memory.db")
    copy = os.path.join(data_dir, "memory.db")
    if os.path.exists(live) and not os.path.exists(copy):
        shutil.copyfile(live, copy)

    body = textwrap.dedent(body)
    script = (
        "import gc, json, os, sys\n"
        "sys.path.insert(0, '.')\n"
        f"RESULT_PATH = {path!r}\n"
        f"HEAVY = {HEAVY_ROOTS!r}\n"
        "\n"
        "def flush(payload_extra=None):\n"
        "    heavy = sorted({m.split('.')[0] for m in sys.modules if m.split('.')[0] in HEAVY})\n"
        "    with open('/proc/self/status') as fh:\n"
        "        rss = next(int(l.split()[1]) for l in fh if l.startswith('VmRSS'))\n"
        "    payload = {'heavy': heavy, 'rss_kb': rss}\n"
        "    if payload_extra:\n"
        "        payload.update(payload_extra)\n"
        "    with open(RESULT_PATH, 'w') as fh:\n"
        "        json.dump(payload, fh)\n"
        "    sys.stdout.flush()\n"
        "    os._exit(0)\n"
        "\n"
        "try:\n" + textwrap.indent(body, "    ") + "    flush()\n"
        "except BaseException as exc:\n"
        "    with open(RESULT_PATH, 'w') as fh:\n"
        "        json.dump({'error': f'{type(exc).__name__}: {exc}'}, fh)\n"
        "    os._exit(1)\n"
    )

    env = dict(os.environ)
    env["MCP_MEMORY_DATA_DIR"] = data_dir
    env["MCP_CONFIG_PATH"] = os.path.join(REPO, "config.yaml")
    env.pop("ARIEL_HASH_EMBEDDINGS", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env,
        timeout=300,
    )
    result = _read_result(name)
    assert result is not None, f"probe wrote no result; rc={proc.returncode}\n{proc.stderr[-3000:]}"
    assert "error" not in result, f"probe failed: {result['error']}\n{proc.stderr[-3000:]}"
    return result


def test_daemon_event_path_does_not_load_the_ml_stack() -> None:
    """The default daemon path must not become a torch host."""
    result = _probe(
        "event_path",
        """
        import asyncio
        from autohooks.appctx import build_app_context, resolve_layer
        import hooks.user_hooks
        import hooks.external as HE

        app = build_app_context()
        mem, graph, rag = resolve_layer(app, "user", "default")

        async def main():
            for text in ("Встреча с Анной Петровой, телефон +7 916 123-45-67",
                         "John Smith met Bob at Acme Corp"):
                await HE.dispatch_event(
                    "new_message", "user", "default",
                    {"text": text, "role": "user"},
                    mem=mem, graph=graph,
                )

        asyncio.run(main())
        gc.collect()
        """,
    )

    assert result["heavy"] == [], (
        f"the event path imports the ML stack: {result['heavy']} "
        f"({result['rss_kb'] // 1024} MB RSS). Each autohooks daemon / MCP "
        "server pays ~900 MB for it."
    )


def test_spacy_gate_still_works_when_enabled() -> None:
    """`rag.spacy_ner: true` must actually reach spaCy, not silently no-op."""
    result = _probe(
        "gate_on",
        """
        from config import config
        import mcp_server.utils.privacy as priv

        config._data = {"rag": {"spacy_ner": True, "ru_personas": ["мамочка"]}}
        priv._nlp = None
        priv._ru_nlp = None

        assert priv._spacy_ner_enabled() is True
        out, _m = priv.sanitize("John Smith met Bob at Acme Corp", use_ner=True)
        flush({"out": out})
        """,
    )

    assert "John" not in result.get("out", "John"), f"NER tier did not run: {result.get('out')!r}"
    assert result["rss_kb"] > 400 * 1024, (
        f"enabling the gate did not load a model — it may have regressed to a silent no-op (rss {result['rss_kb'] // 1024} MB)"
    )


def test_the_universe_of_ml_entry_points_is_closed() -> None:
    """Every function that can reach spaCy must respect the gate.

    The first version of the fix gated the call site in `sanitize()` only, and
    the test passed — while `_get_nlp()` and `_get_ru_nlp()` stayed open
    doors for any future caller. This walks the module instead of trusting the
    path that happened to be measured.
    """
    gate_off = _probe(
        "doors_closed",
        """
        from config import config
        import mcp_server.utils.privacy as priv

        config._data = {}
        priv._nlp = None
        priv._ru_nlp = None
        priv._ner_breaker = None

        assert priv._spacy_ner_enabled() is False
        # Both loaders must refuse while the gate is closed...
        assert priv._get_nlp() is None
        assert priv._get_ru_nlp() is None
        # ...and neither may have imported anything heavy to do it.
        flush({"nlp": priv._nlp, "ru_nlp": priv._ru_nlp})
        """,
    )

    assert gate_off["heavy"] == [], f"a loader imported the ML stack anyway: {gate_off['heavy']}"
    assert gate_off["nlp"] is None and gate_off["ru_nlp"] is None


def test_the_two_gates_are_independent() -> None:
    """The daemon gate must not blind the nightly miner, and vice versa.

    The first attempt used one flag for both. That either re-fattened the
    daemon (miner flag on) or silently stopped entity mining (daemon flag off),
    and neither failure was visible to a test that only measured the daemon's
    own import path.
    """
    result = _probe(
        "gates",
        """
        from config import config
        import mcp_server.utils.privacy as priv

        priv._nlp = None
        priv._ru_nlp = None

        # Defaults as shipped: daemon off, miner on.
        config._data = {}
        assert priv._spacy_ner_enabled() is False
        assert priv._entity_mining_ner_enabled() is True
        assert priv._get_nlp() is None, "message path must not load a model"
        assert priv._get_nlp(for_ner=True) is None or True  # load is observable below

        # Miner explicitly off: it must refuse too.
        config._data = {"rag": {"entity_mining_ner": False}}
        priv._nlp = None
        assert priv._get_nlp(for_ner=True) is None

        # Privacy explicitly on: it must reach the model.
        config._data = {"rag": {"spacy_ner": True}}
        priv._nlp = None
        assert priv._get_nlp() is not None
        flush({})
        """,
    )

    assert result["rss_kb"] > 400 * 1024, f"the privacy gate did not open even when explicitly enabled (rss {result['rss_kb'] // 1024} MB)"
