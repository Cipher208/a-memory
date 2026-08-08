import pytest
from pathlib import Path
from wiki import WikiParser, WikiEntry

def test_parse_full_md_with_frontmatter():
    md_content = """---
title: "Test Entry"
wiki_type: "concept"
tags: ["test", "wiki"]
importance: 0.8
created_at: 1234567890.0
updated_at: 1234567890.0
---
This is the content."""
    
    entry = WikiParser.parse(md_content)
    
    assert entry.title == "Test Entry"
    assert entry.wiki_type == "concept"
    assert entry.tags == ["test", "wiki"]
    assert entry.importance == 0.8
    assert entry.content == "This is the content."
    assert entry.created_at == 1234567890.0

def test_parse_plain_md_no_frontmatter():
    md_content = "Just plain content."
    file_path = Path("my_note.md")
    
    entry = WikiParser.parse(md_content, file_path=file_path)
    
    assert entry.title == "my_note"
    assert entry.wiki_type == "note"
    assert entry.content == "Just plain content."
    assert entry.importance == 0.5
    assert entry.tags == []

def test_parse_malformed_yaml():
    md_content = """---
title: "Test
importance: invalid
---
Content"""
    # python-frontmatter is usually lenient or returns partial metadata
    # We should verify it doesn't crash and handles defaults
    entry = WikiParser.parse(md_content)
    # If YAML is malformed, frontmatter.loads fails and we fallback to full text
    assert "Content" in entry.content
    assert entry.importance == 0.5 # default

def test_to_markdown():
    entry = WikiEntry(
        wiki_type="task",
        title="My Task",
        content="Do things.",
        file_path="task.md",
        tags=["urgent"],
        importance=1.0,
        created_at=100.0,
        updated_at=200.0
    )
    
    md = WikiParser.to_markdown(entry)
    assert "title: My Task" in md
    assert "wiki_type: task" in md
    assert "Do things." in md
    
    # Round trip
    parsed = WikiParser.parse(md)
    assert parsed.title == entry.title
    assert parsed.content == entry.content
    assert parsed.importance == entry.importance
