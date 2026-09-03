"""A1.7: docs → markdown → wiki pipeline (conversion + collection logic)."""

import pytest

from scripts.docs_to_wiki import collect, html_to_md, to_markdown


@pytest.fixture()
def docs_tree(tmp_path):
    src = tmp_path / "docs"
    (src / "guides").mkdir(parents=True)
    (src / ".git").mkdir()  # junk dir: explicit create
    (src / "guides" / "deploy.md").write_text("# Deploy\n\nStep one.", encoding="utf-8")
    (src / "legacy.html").write_text(
        "<html><head><style>body{}</style></head><body>"
        "<h1>Old Page</h1><p>Some <b>bold</b> and <a href='x.html'>link</a>.</p>"
        "<pre><code>run_me()</code></pre>"
        "<script>evil()</script></body></html>",
        encoding="utf-8",
    )
    (src / "notes.txt").write_text("plain notes", encoding="utf-8")
    (src / ".git" / "hidden.md").write_text("junk", encoding="utf-8")
    (src / "binary.py").write_text("print(1)", encoding="utf-8")  # not a doc ext
    return src


def test_collect_filters_ext_and_junk(docs_tree):
    files = collect(docs_tree)
    names = [f.name for f in files]
    assert names == ["deploy.md", "legacy.html", "notes.txt"]  # sorted; .git hidden, .py excluded


def test_md_passthrough(docs_tree):
    assert to_markdown(docs_tree / "guides" / "deploy.md").startswith("# Deploy")


def test_html_to_md_with_markdownify(docs_tree, monkeypatch):
    md = to_markdown(docs_tree / "legacy.html")
    assert "# Old Page" in md
    assert "**bold**" in md
    assert "evil()" not in md  # script dropped
    assert "[link](x.html)" in md
    assert "run_me()" in md  # pre/code kept


def test_html_to_md_regex_fallback(docs_tree, monkeypatch):
    """Without markdownify the stdlib pass still produces sane markdown."""
    import builtins

    real_import = builtins.__import__

    def _no_markdownify(name, *args, **kwargs):
        if name == "markdownify":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_markdownify)
    md = html_to_md((docs_tree / "legacy.html").read_text())
    assert "# Old Page" in md
    assert "**bold**" in md
    assert "evil()" not in md
    assert "`run_me()`" in md


def test_convert_to_ssot_writes_pages(docs_tree, tmp_path):
    from scripts.docs_to_wiki import convert_to_ssot

    ssot = tmp_path / "ssot"
    pages = convert_to_ssot(docs_tree, ssot)
    assert len(pages) == 3
    assert (ssot / "guides" / "deploy.md").exists()
    assert (ssot / "legacy.html").with_suffix(".md").exists()
