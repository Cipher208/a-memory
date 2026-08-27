"""Wiki schema lint — 6 checks (frontmatter, required, wikilinks, length, index, tags).

Pure module; consumed by `WikiManager.add()` and `sync_external()` as a
side-effect. Default behavior is warning-only; opt-in `auto_fix=True`
on the manager only creates missing INDEX.md stubs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — runtime: callers pass Path instances (lint_wiki_layer, lint_missing_index)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import WikiEntry

logger = logging.getLogger(__name__)


# ── Vocabulary ───────────────────────────────────────────────────────

# 8 baseline tags. Plus enabled wiki_type names (layer-aware).
# Tag is "known" if it is in this set OR equals an enabled wiki_type.
WIKI_LINT_TAG_BASELINE: frozenset[str] = frozenset(
    {
        "decision",
        "learning",
        "todo",
        "principle",
        "context",
        "spec",
        "draft",
    }
)


def wiki_lint_tag_vocabulary(enabled_types: list[str]) -> frozenset[str]:
    """Return baseline + enabled wiki_type names. Layer-aware."""
    return WIKI_LINT_TAG_BASELINE | frozenset(enabled_types)


# ── Findings ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    code: str  # e.g. "missing_title", "broken_wikilink"
    message: str  # human-readable
    location: str  # "title", "wikilink:[[X]]", etc.
    fixable: bool = False  # can auto_fix handle this?
    severity: str = "warning"  # "warning" | "error"


@dataclass
class LintReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    @property
    def fixable_count(self) -> int:
        return sum(1 for f in self.findings if f.fixable)

    def by_code(self, code: str) -> list[Finding]:
        return [f for f in self.findings if f.code == code]


# ── Wikilink regex (Obsidian-style) ─────────────────────────────────

# [[target]] or [[target|alias]] — group 1 = target name
_WIKILINK_RE = re.compile(r"\[\[([^\]|\n]+)(?:\|[^\]\n]+)?\]\]")


# ── Per-entry check ──────────────────────────────────────────────────


def lint_entry(
    entry: WikiEntry,
    *,
    all_titles: set[str] | None = None,
    enabled_types: list[str] | None = None,
) -> list[Finding]:
    """Run all 6 checks against a single WikiEntry. Never raises."""
    findings: list[Finding] = []
    titles = all_titles or set()
    types = enabled_types or []

    # 1. frontmatter_malformed: parser sets metadata={} on ScannerError
    #    but the entry came from a successful parse, so this is empty
    #    in practice unless upstream callers bypass the parser. We
    #    detect "default wiki_type" as a proxy: if the parser fell back
    #    to "note" (the default) AND file has no frontmatter signal,
    #    we cannot know here. So we rely on check #2 instead.

    # 2. required fields: title or wiki_type fell back to default
    if entry.title == "Untitled":
        findings.append(
            Finding(
                code="missing_title",
                message="title fell back to 'Untitled' (no frontmatter title, no filename stem)",
                location="title",
                fixable=False,
            )
        )
    if entry.wiki_type == "note" and not getattr(entry, "_explicit_type", False) and not entry.content.strip():
        # If parser got "note" via default, flag it. We can't distinguish
        # from an explicit "note" wiki_type here without a marker; for
        # now, only flag if entry's tags or content is also empty.
        findings.append(
            Finding(
                code="missing_wiki_type",
                message="wiki_type fell back to 'note' default and content is empty",
                location="wiki_type",
                fixable=False,
            )
        )

    # 3. broken wikilinks
    if entry.content and "[[" in entry.content:
        for m in _WIKILINK_RE.finditer(entry.content):
            target = m.group(1).strip()
            if target and titles and target not in titles:
                findings.append(
                    Finding(
                        code="broken_wikilink",
                        message=f"[[{target}]] has no matching wiki page",
                        location=f"wikilink:[[{target}]]",
                        fixable=False,
                    )
                )

    # 4. page length cap (50k chars = ~12k tokens)
    if len(entry.content) > 50_000:
        findings.append(
            Finding(
                code="page_too_long",
                message=f"content is {len(entry.content)} chars (>50000 cap)",
                location="content",
                fixable=False,
            )
        )

    # 6. unknown tags
    if entry.tags:
        vocab = wiki_lint_tag_vocabulary(types)
        for tag in entry.tags:
            if tag not in vocab:
                findings.append(
                    Finding(
                        code="unknown_tag",
                        message=f"tag '{tag}' not in vocabulary (baseline + enabled types)",
                        location=f"tag:{tag}",
                        fixable=False,
                    )
                )

    return findings


# ── Missing-index check (whole layer) ──────────────────────────────


def lint_missing_index(wiki_type_dir: Path) -> Finding | None:
    """Return a Finding if `wiki_type_dir` has no INDEX.md.

    Single check scoped to one type directory (called per type, not per entry).
    """
    index_path = wiki_type_dir / "INDEX.md"
    if not index_path.exists():
        return Finding(
            code="missing_index",
            message=f"no INDEX.md in {wiki_type_dir.name}/",
            location=str(wiki_type_dir),
            fixable=True,
        )
    return None


# ── Layer scan ───────────────────────────────────────────────────────


def lint_wiki_layer(
    layer: str,
    base_dir: Path,
    enabled_types: list[str],
    *,
    auto_fix: bool = False,
) -> LintReport:
    """Scan every .md in `base_dir/<wiki_type>/`. Returns a LintReport.

    `auto_fix=True` creates missing INDEX.md stubs (the only fixable check).
    """
    report = LintReport()
    all_titles: set[str] = set()

    # First pass: collect titles for wikilink resolution
    for wiki_type in enabled_types:
        type_dir = base_dir / wiki_type
        if not type_dir.is_dir():
            if auto_fix:
                type_dir.mkdir(parents=True, exist_ok=True)
                # Newly-created dir has no INDEX.md yet — flag it
                f = lint_missing_index(type_dir)
                if f:
                    report.findings.append(f)
            continue
        for md_file in type_dir.glob("*.md"):
            if md_file.name == "INDEX.md":
                continue
            # Use filename stem as title proxy (matches parser fallback)
            all_titles.add(md_file.stem)

    # Second pass: lint every .md
    for wiki_type in enabled_types:
        type_dir = base_dir / wiki_type
        if not type_dir.is_dir():
            continue
        # Per-type missing_index
        f = lint_missing_index(type_dir)
        if f:
            report.findings.append(f)
            if auto_fix:
                _write_index_stub(type_dir, wiki_type, all_titles)
        # Per-entry
        from .parser import WikiParser  # local import to avoid cycle

        for md_file in type_dir.glob("*.md"):
            if md_file.name == "INDEX.md":
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
                entry = WikiParser.parse(text, md_file)
            except Exception as exc:
                logger.warning("wiki lint: failed to parse %s: %s", md_file, exc)
                continue
            entry_findings = lint_entry(
                entry,
                all_titles=all_titles,
                enabled_types=enabled_types,
            )
            report.findings.extend(entry_findings)

    return report


# ── Auto-fix ─────────────────────────────────────────────────────────

# Codes that auto_fix handles. Content-level fixes are deliberately NOT here.
_AUTO_FIXABLE_CODES = frozenset({"missing_index"})


def auto_fix_entry(
    entry: WikiEntry,
    findings: list[Finding],
) -> tuple[WikiEntry, list[str]]:
    """Apply safe auto-fixes.

    Currently only creates missing INDEX.md (which is per-type-dir, not
    per-entry, so this is a no-op for entries). Kept as an extension
    point for future per-entry fixes.

    Returns (possibly-mutated entry, list of fix descriptions).
    """
    fixes: list[str] = []
    # Per-entry: no current fixable checks (missing_index is per-type-dir)
    return entry, fixes


def _write_index_stub(type_dir: Path, wiki_type: str, titles: set[str]) -> None:
    """Create a minimal INDEX.md listing all pages in `type_dir`."""
    index_path = type_dir / "INDEX.md"
    if index_path.exists():
        return  # idempotent
    body_lines = [
        f"# {wiki_type} Index",
        "",
        "Auto-generated by `wiki.lint.lint_wiki_layer(..., auto_fix=True)`.",
        "",
        "## Pages",
        "",
    ]
    for t in sorted(titles):
        body_lines.append(f"- {t}")
    body_lines.append("")
    index_path.write_text("\n".join(body_lines), encoding="utf-8")
    logger.info("wiki lint: created %s", index_path)


def auto_fix_type_dirs(base_dir: Path, enabled_types: list[str]) -> list[str]:
    """Create INDEX.md stubs in all type dirs that lack one.

    Returns list of created paths as strings.
    """
    created: list[str] = []
    for wiki_type in enabled_types:
        type_dir = base_dir / wiki_type
        if not type_dir.is_dir():
            type_dir.mkdir(parents=True, exist_ok=True)
        if not (type_dir / "INDEX.md").exists():
            _write_index_stub(type_dir, wiki_type, set())
            created.append(str(type_dir / "INDEX.md"))
    return created
