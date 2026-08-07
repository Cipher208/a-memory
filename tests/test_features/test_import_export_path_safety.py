"""Tests for import_export path traversal prevention."""

import pytest

from features.import_export import ImportExport


@pytest.fixture
def ie(tmp_path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir()

    class FakeCM:
        def __init__(self, base):
            self._base = base

        @property
        def base_dir(self):
            return self._base

    obj = ImportExport.__new__(ImportExport)
    obj._cm = FakeCM(tmp_path)
    obj.export_dir = export_dir
    return obj


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd"])
def test_import_rejects_traversal(ie, path):
    import asyncio

    with pytest.raises(ValueError, match="escapes base directory"):
        asyncio.run(ie.import_user(path))
