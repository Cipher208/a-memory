"""Repo root must win over same-named pretenders on sys.path.

Ground: in production the gateway spawns `python -m autohooks` with its own
cwd, so our `mcp_server/` package resolves only through whatever happens to
precede it on sys.path. A same-named `mcp_server.py` from an unrelated
package (Browser Use, inside a PM-managed 3.14 env) won the race several
times, and its import chain died on a native-extension mismatch. The CLI
must pin our own tree first, deterministically, instead of hoping.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)


def test_repo_root_first_despite_pretender(monkeypatch, tmp_path):
    poison = tmp_path / "poison"
    poison.mkdir()
    (poison / "mcp_server.py").write_text('raise ImportError("poisoned pretender")\n')
    monkeypatch.syspath_prepend(str(poison))
    for mod in ("mcp_server", "autohooks.__main__"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    importlib.invalidate_caches()

    import autohooks.__main__  # noqa: F401  -- runs the path guard at top

    spec = importlib.util.find_spec("mcp_server")
    assert spec is not None and spec.origin is not None
    assert spec.origin.startswith(REPO_ROOT), spec.origin
