"""Tests for chunk_text with overlap."""

import pytest

from rag.chunking import chunk_text


def test_overlap_param_now_used():
    text = "Paragraph one is here.\n\n" * 30
    chunks = chunk_text(text, max_size=200, overlap=50)
    assert all(len(c) <= 230 for c in chunks)
    overlaps = sum(1 for a, b in zip(chunks, chunks[1:], strict=False) if any(line in b for line in a.split("\n\n") if line))
    assert overlaps >= len(chunks) - 1


def test_overlap_validation():
    with pytest.raises(ValueError):
        chunk_text("x", max_size=100, overlap=100)


def test_long_paragraph_word_split():
    long_para = " ".join(["word"] * 300)
    chunks = chunk_text(long_para, max_size=100, overlap=20)
    assert all(len(c) <= 120 for c in chunks)
    assert sum(len(c.split()) for c in chunks) >= 300
