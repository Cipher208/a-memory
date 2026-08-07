"""
Markdown Parser for Wiki — handles YAML frontmatter and content separation.
"""

import re
from typing import Any

import yaml


class WikiParser:
    """Parses and generates Markdown files with YAML frontmatter."""

    @staticmethod
    def parse(text: str) -> dict[str, Any]:
        """Parse .md with YAML frontmatter.

        Returns dict with: title, content, tags, importance.
        """
        result: dict[str, Any] = {"title": "", "content": text, "tags": [], "importance": 0.5}

        if not text.startswith("---"):
            return result

        try:
            parts = re.split(r"^---\s*$", text, maxsplit=2, flags=re.MULTILINE)
            if len(parts) >= 3:
                frontmatter = yaml.safe_load(parts[1])
                if isinstance(frontmatter, dict):
                    result["title"] = frontmatter.get("title", "")
                    result["tags"] = frontmatter.get("tags", [])
                    result["importance"] = float(frontmatter.get("importance", 0.5))
                    result["content"] = parts[2].strip()
        except (yaml.YAMLError, ValueError, TypeError):
            # Fallback to plain text if YAML is malformed or types are wrong
            pass

        return result

    @staticmethod
    def to_md(title: str, content: str, tags: list[str] | None = None, importance: float = 0.5) -> str:
        """Generate .md string with YAML frontmatter."""
        lines = ["---"]
        lines.append(f"title: {title}")
        if tags:
            lines.append(f"tags: {tags}")
        lines.append(f"importance: {importance}")
        lines.append("---")
        lines.append("")
        lines.append(content)
        return "\n".join(lines)
