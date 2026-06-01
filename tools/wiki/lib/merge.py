from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .frontmatter import Page, parse_page
from .hash_source import display_path
from .paths import resolve_report_path
from .review import create_review_item, list_review_items


@dataclass(frozen=True)
class MergeCandidate:
    key: str
    reason: str
    pages: list[Path]


@dataclass(frozen=True)
class MergeScanResult:
    candidates: list[MergeCandidate]
    report_path: Path | None
    review_path: Path | None


def scan_merge_candidates(
    root: Path,
    report_path: Path | None = None,
    create_review: bool = False,
) -> MergeScanResult:
    candidates = _find_merge_candidates(root)
    if report_path is not None:
        report_path = resolve_report_path(root, report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_merge_report(root, candidates))

    review_path = None
    if create_review and candidates:
        related_pages = sorted(
            {
                display_path(path, root)
                for candidate in candidates
                for path in candidate.pages
            }
        )
        existing = _existing_merge_review(root, related_pages)
        if existing is not None:
            review_path = existing
        else:
            item = create_review_item(
                root,
                review_type="merge",
                summary="Merge Candidates",
                related_pages=related_pages,
                context=_merge_review_context(root, candidates),
            )
            review_path = item.path
    return MergeScanResult(candidates=candidates, report_path=report_path, review_path=review_path)


def _existing_merge_review(root: Path, related_pages: list[str]) -> Path | None:
    expected = sorted(related_pages)
    for item in list_review_items(root):
        if item.review_type != "merge" or item.status != "pending":
            continue
        if item.summary != "Merge Candidates":
            continue
        if sorted(item.related_pages) == expected:
            return item.path
    return None


def render_merge_report(root: Path, candidates: list[MergeCandidate]) -> str:
    lines = [
        f"# Merge Proposal Report - {date.today().isoformat()}",
        "",
        "## Summary",
        "",
        f"- Candidate groups: {len(candidates)}",
        "- Automatic merge performed: no",
        "- Required action: review candidate groups and resolve through `review` workflow when judgment is needed.",
        "",
        "## Candidates",
        "",
    ]
    if not candidates:
        lines.append("- None.")
        lines.append("")
        return "\n".join(lines)

    for candidate in candidates:
        lines.extend(
            [
                f"### {candidate.key}",
                "",
                f"- Reason: {candidate.reason}",
                "- Pages:",
            ]
        )
        lines.extend(f"  - `{display_path(path, root)}`" for path in candidate.pages)
        lines.append("")
    return "\n".join(lines)


def _find_merge_candidates(root: Path) -> list[MergeCandidate]:
    grouped: dict[str, set[Path]] = {}
    reasons: dict[str, str] = {}
    for page in _wiki_pages(root):
        if not page.frontmatter:
            continue
        status = page.frontmatter.get("status")
        if status == "deprecated":
            continue
        title = str(page.frontmatter.get("title", "")).strip()
        for label, reason in _page_merge_keys(title, page.frontmatter.get("aliases", [])):
            grouped.setdefault(label, set()).add(page.path)
            reasons.setdefault(label, reason)

    candidates = [
        MergeCandidate(key=key, reason=reasons[key], pages=sorted(paths))
        for key, paths in grouped.items()
        if len(paths) > 1
    ]
    return sorted(candidates, key=lambda candidate: candidate.key)


def _wiki_pages(root: Path) -> list[Page]:
    return [parse_page(path) for path in sorted((root / "wiki").rglob("*.md"))]


def _page_merge_keys(title: str, aliases: object) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    title_key = _normalize_key(title)
    if title_key:
        keys.append((f"title:{title_key}", "same normalized title"))
    if isinstance(aliases, list):
        for alias in aliases:
            alias_key = _normalize_key(str(alias))
            if alias_key:
                keys.append((f"alias:{alias_key}", "shared alias"))
    return keys


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _merge_review_context(root: Path, candidates: list[MergeCandidate]) -> str:
    lines = [
        "Deterministic duplicate scan found merge candidates. No automatic merge was performed.",
        "",
        "| Key | Reason | Pages |",
        "|---|---|---|",
    ]
    for candidate in candidates:
        pages = ", ".join(f"`{display_path(path, root)}`" for path in candidate.pages)
        lines.append(f"| {candidate.key} | {candidate.reason} | {pages} |")
    return "\n".join(lines)
