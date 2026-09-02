"""E1: atomic L1 persistence — no truncated JSON, no .tmp leftovers, prod wiring."""

import json
from pathlib import Path

from core.reflex import ReflexBuffer


def test_save_is_atomic_no_tmp_leftover(tmp_path):
    p = tmp_path / "l1_user_default.json"
    buf = ReflexBuffer(max_size=50, persist_path=str(p))
    for i in range(10):
        buf.add("user", f"msg {i}", tokens=1)  # debounced: first add saves, then every 10
    buf._save()  # flush the tail deterministically
    assert p.exists()
    assert not list(tmp_path.glob("*.tmp")), "temp file must be renamed away"
    data = json.loads(p.read_text())
    assert len(data) == 10


def test_truncated_file_loads_silently(tmp_path):
    p = tmp_path / "l1.json"
    p.write_text('{"role": "user", "cont')  # crash-truncated JSON
    buf = ReflexBuffer(max_size=50, persist_path=str(p))
    assert buf.size() == 0  # swallowed, buffer starts clean
    buf.add("user", "recovered", tokens=1)
    buf.add("user", "now valid", tokens=1)
    buf._save()
    assert len(json.loads(p.read_text())) == 2  # file healed on next save


def test_restore_roundtrip(tmp_path):
    p = tmp_path / "l1.json"
    buf = ReflexBuffer(max_size=5, persist_path=str(p))
    for i in range(7):
        buf.add("user", f"m{i}", tokens=1)
    buf._save()
    buf2 = ReflexBuffer(max_size=5, persist_path=str(p))
    buf2.restore([ReflexEntry(**e) for e in json.loads(p.read_text())])
    assert [e.content for e in buf2.get_full()] == [f"m{i}" for i in range(2, 7)]  # maxlen honored


from core.reflex import ReflexEntry  # noqa: E402 — used above by test_restore_roundtrip


def test_memory_layer_wires_persist_path(tmp_path, monkeypatch):
    import core

    monkeypatch.setattr(core.connection_manager, "base_dir", tmp_path)
    layer = core.MemoryLayer("user", "default")
    assert layer.l1.persist_path is not None
    assert str(layer.l1.persist_path).startswith(str(tmp_path))
    layer.l1.add("user", "hello", tokens=1)
    layer.l1._save()
    # fresh layer round-trips through the file
    layer2 = core.MemoryLayer("user", "default")
    assert layer2.l1.get_full()[-1].content == "hello"


def test_memory_layer_sanitizes_hostile_user_id(tmp_path, monkeypatch):
    import core

    monkeypatch.setattr(core.connection_manager, "base_dir", tmp_path)
    layer = core.MemoryLayer("user", "../../evil")
    name = Path(str(layer.l1.persist_path)).name
    assert "/" not in name
    assert "\\" not in name
