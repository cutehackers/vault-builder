from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .frontmatter import Page, parse_page, render_page
from .hash_source import display_path
from .lint import find_human_locks
from .paths import resolve_report_path
from .review import ReviewItem, list_review_items
from .source_registry import SourceRegistryEntry, build_source_registry
from .wiki_update import (
    append_log_entry,
    apply_index_entry,
    assert_can_append_log_entry,
    assert_can_apply_index_entry,
    render_index_entry_update,
)


MAP_DEFINITIONS = {
    "topic-map": {
        "title": "Topic Map",
        "summary": "Generated navigation map grouping durable wiki pages by type and tag.",
        "tags": ["map", "navigation", "topics"],
    },
    "source-map": {
        "title": "Source Map",
        "summary": "Generated navigation map for raw source registration and source pages.",
        "tags": ["map", "navigation", "sources"],
    },
    "decision-map": {
        "title": "Decision Map",
        "summary": "Generated navigation map for durable decisions.",
        "tags": ["map", "navigation", "decisions"],
    },
    "review-map": {
        "title": "Unresolved Review Map",
        "summary": "Generated navigation map for pending human-review items.",
        "tags": ["map", "navigation", "review"],
    },
    "lifecycle-map": {
        "title": "Lifecycle Map",
        "summary": "Generated navigation map for stale, contested, deprecated, and source-attention signals.",
        "tags": ["map", "navigation", "lifecycle"],
    },
}


@dataclass(frozen=True)
class NavigationMapBuildResult:
    target_paths: list[Path]
    changed_paths: list[Path]
    report_path: Path | None
    dry_run: bool
    check: bool


def build_navigation_maps(
    root: Path,
    report_path: Path | None = None,
    dry_run: bool = False,
    check: bool = False,
    build_date: date | None = None,
) -> NavigationMapBuildResult:
    current_date = build_date or date.today()
    wiki_root = root / "wiki"
    pages = _load_wiki_pages(wiki_root)
    source_entries = build_source_registry(root, checked_at=current_date)
    review_items = list_review_items(root)

    rendered_pages = _render_map_pages(root, pages, source_entries, review_items, current_date)
    target_paths = [path for path, _content in rendered_pages]
    changed_paths = _changed_paths(rendered_pages)
    index_entries = [
        {
            "section": "Maps",
            "target": f"maps/{slug}",
            "summary": str(definition["summary"]),
        }
        for slug, definition in MAP_DEFINITIONS.items()
    ]
    index_changed_targets = [
        str(entry["target"])
        for entry in index_entries
        if render_index_entry_update(wiki_root / "index.md", entry) is not None
    ]
    log_items = [f"Updated [[maps/{path.stem}]]." for path in changed_paths]
    log_items.extend(f"Registered [[{target}]] in index." for target in index_changed_targets)

    if not dry_run and not check:
        for index_entry in index_entries:
            assert_can_apply_index_entry(wiki_root / "index.md", index_entry)
        if log_items:
            assert_can_append_log_entry(
                wiki_root / "log.md",
                {
                    "event_type": "repair",
                    "title": "Rebuild Navigation Maps",
                    "items": log_items,
                },
            )
        _write_map_pages(root, rendered_pages)
        for index_entry in index_entries:
            apply_index_entry(wiki_root / "index.md", index_entry)
        if log_items:
            append_log_entry(
                wiki_root / "log.md",
                {
                    "event_type": "repair",
                    "title": "Rebuild Navigation Maps",
                    "items": log_items,
                },
            )

    if report_path is not None:
        report_path = resolve_report_path(root, report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_render_maps_report(root, target_paths, changed_paths, dry_run, check, current_date))

    return NavigationMapBuildResult(
        target_paths=target_paths,
        changed_paths=changed_paths,
        report_path=report_path,
        dry_run=dry_run,
        check=check,
    )


def _load_wiki_pages(wiki_root: Path) -> list[Page]:
    pages: list[Page] = []
    for path in sorted(wiki_root.rglob("*.md")):
        page = parse_page(path)
        if page.frontmatter is None:
            continue
        pages.append(page)
    return pages


def _render_map_pages(
    root: Path,
    pages: list[Page],
    source_entries: list[SourceRegistryEntry],
    review_items: list[ReviewItem],
    current_date: date,
) -> list[tuple[Path, str]]:
    wiki_root = root / "wiki"
    topic_pages = _pages_with_generated_map_pages(root, pages, current_date)
    bodies = {
        "topic-map": _render_topic_map(root, topic_pages),
        "source-map": _render_source_map(root, source_entries),
        "decision-map": _render_decision_map(root, pages),
        "review-map": _render_review_map(root, review_items),
        "lifecycle-map": _render_lifecycle_map(root, pages, source_entries),
    }
    rendered: list[tuple[Path, str]] = []
    for slug, body in bodies.items():
        target = wiki_root / "maps" / f"{slug}.md"
        if target.exists() and find_human_locks(parse_page(target)).page_locked:
            raise ValueError(f"Map page is human-locked: {display_path(target, root)}")
        definition = MAP_DEFINITIONS[slug]
        frontmatter = _map_frontmatter(slug, definition, target, current_date)
        rendered.append(
            (
                target,
                _render_stable_map_page(target, frontmatter, body),
            )
        )
    return rendered


def _pages_with_generated_map_pages(root: Path, pages: list[Page], current_date: date) -> list[Page]:
    wiki_root = root / "wiki"
    generated_paths = {wiki_root / "maps" / f"{slug}.md" for slug in MAP_DEFINITIONS}
    navigable_pages = [page for page in pages if page.path not in generated_paths]
    for slug, definition in MAP_DEFINITIONS.items():
        target = wiki_root / "maps" / f"{slug}.md"
        navigable_pages.append(
            Page(
                path=target,
                frontmatter=_map_frontmatter(slug, definition, target, current_date),
                body="",
            )
        )
    return navigable_pages


def _render_stable_map_page(target: Path, frontmatter: dict[str, Any], body: str) -> str:
    if target.exists():
        existing = parse_page(target)
        existing_updated = (existing.frontmatter or {}).get("updated")
        if existing_updated:
            stable_frontmatter = dict(frontmatter)
            stable_frontmatter["updated"] = str(existing_updated)
            stable_content = render_page(stable_frontmatter, body)
            if target.read_text() == stable_content:
                return stable_content
    return render_page(frontmatter, body)


def _map_frontmatter(slug: str, definition: dict[str, Any], target: Path, current_date: date) -> dict[str, Any]:
    created = current_date.isoformat()
    if target.exists():
        existing = parse_page(target)
        if existing.frontmatter and existing.frontmatter.get("created"):
            created = str(existing.frontmatter["created"])
    return {
        "title": definition["title"],
        "type": "map",
        "status": "active",
        "created": created,
        "updated": current_date.isoformat(),
        "owner": "agent",
        "summary": definition["summary"],
        "source_count": 0,
        "tags": definition["tags"],
        "related": ["index", "maps/wiki-operating-map"],
        "confidence": "high",
        "quality": {
            "provenance": "section",
            "links": "unchecked",
            "contradictions": "none",
            "review_required": False,
        },
        "generated_by": "tools/wiki/cli.py maps build",
        "map_slug": slug,
    }


def _render_topic_map(root: Path, pages: list[Page]) -> str:
    navigable = _navigable_pages(root, pages)
    lines = [
        "# Topic Map",
        "",
        "## Purpose",
        "",
        "This generated map groups durable wiki pages by page type and tag.",
        "",
        "Source: Deterministic map build from wiki frontmatter.",
        "",
        "## By Type",
        "",
    ]
    by_type: dict[str, list[Page]] = {}
    for page in navigable:
        by_type.setdefault(str(page.frontmatter.get("type", "unknown")), []).append(page)
    for page_type in sorted(by_type):
        lines.extend([f"### {page_type.title()}", ""])
        lines.extend(_page_lines(root, by_type[page_type]))
        lines.append("")

    lines.extend(["## By Tag", ""])
    by_tag: dict[str, list[Page]] = {}
    for page in navigable:
        for tag in _as_string_list(page.frontmatter.get("tags", [])):
            by_tag.setdefault(tag, []).append(page)
    if not by_tag:
        lines.extend(["- No tagged pages.", ""])
    for tag in sorted(by_tag):
        lines.extend([f"### {tag}", ""])
        lines.extend(_page_lines(root, by_tag[tag]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_source_map(root: Path, entries: list[SourceRegistryEntry]) -> str:
    lines = [
        "# Source Map",
        "",
        "## Purpose",
        "",
        "This generated map shows raw source registration state and source pages.",
        "",
        "Source: Deterministic map build from raw/source-page registry metadata.",
        "",
        "## Source Registry",
        "",
    ]
    if not entries:
        lines.extend(["- No raw sources registered or discovered.", ""])
        return "\n".join(lines)

    for entry in entries:
        source_page = _source_page_link(entry.source_page)
        lines.append(
            f"- `{entry.raw_path}` — status: {entry.ingest_status}; kind: {entry.source_kind}; "
            f"page: {source_page}; sha256: `{entry.raw_sha256 or 'missing'}`"
        )
    lines.append("")
    return "\n".join(lines)


def _render_decision_map(root: Path, pages: list[Page]) -> str:
    decisions = [page for page in pages if _page_type(page) == "decision"]
    lines = [
        "# Decision Map",
        "",
        "## Purpose",
        "",
        "This generated map lists durable decision pages by status.",
        "",
        "Source: Deterministic map build from decision page frontmatter.",
        "",
    ]
    if not decisions:
        lines.extend(["## Decisions", "", "- No decision pages yet.", ""])
        return "\n".join(lines)

    by_status: dict[str, list[Page]] = {}
    for page in decisions:
        by_status.setdefault(str(page.frontmatter.get("status", "unknown")), []).append(page)
    for status in sorted(by_status):
        lines.extend([f"## {status.title()} Decisions", ""])
        lines.extend(_page_lines(root, by_status[status]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_review_map(root: Path, review_items: list[ReviewItem]) -> str:
    pending = [item for item in review_items if item.status == "pending"]
    lines = [
        "# Unresolved Review Map",
        "",
        "## Purpose",
        "",
        "This generated map lists pending human-review queue items.",
        "",
        "Source: Deterministic map build from `scratch/review/` frontmatter.",
        "",
        "## Pending Reviews",
        "",
    ]
    if not pending:
        lines.extend(["- No unresolved review items.", ""])
        return "\n".join(lines)

    for item in pending:
        related = ", ".join(_related_page_link(ref) for ref in item.related_pages) or "none"
        lines.append(
            f"- {item.review_type} | `{display_path(item.path, root)}` | {item.summary} | related: {related}"
        )
    lines.append("")
    return "\n".join(lines)


def _render_lifecycle_map(root: Path, pages: list[Page], entries: list[SourceRegistryEntry]) -> str:
    deprecated = [page for page in pages if str(page.frontmatter.get("status")) == "deprecated"]
    contested = [
        page
        for page in pages
        if str(page.frontmatter.get("status")) == "contested"
        or _quality_value(page, "contradictions") in {"present", "contested"}
        or _quality_value(page, "review_required") is True
    ]
    attention_sources = [
        entry for entry in entries if entry.ingest_status in {"drift", "missing-source", "unregistered"}
    ]
    lines = [
        "# Lifecycle Map",
        "",
        "## Purpose",
        "",
        "This generated map highlights stale, contested, deprecated, and source-attention signals.",
        "",
        "Source: Deterministic map build from wiki frontmatter and source registry metadata.",
        "",
        "## Deprecated Pages",
        "",
    ]
    lines.extend(_page_lines(root, deprecated) if deprecated else ["- None."])
    lines.extend(["", "## Contested Or Review-Required Pages", ""])
    lines.extend(_page_lines(root, contested) if contested else ["- None."])
    lines.extend(["", "## Source Attention", ""])
    if attention_sources:
        for entry in attention_sources:
            lines.append(f"- `{entry.raw_path}` — {entry.ingest_status}; source page: {_source_page_link(entry.source_page)}")
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def _write_map_pages(root: Path, rendered_pages: list[tuple[Path, str]]) -> None:
    for target_path, content in rendered_pages:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content)


def _changed_paths(rendered_pages: list[tuple[Path, str]]) -> list[Path]:
    changed: list[Path] = []
    for target_path, content in rendered_pages:
        if not target_path.exists() or target_path.read_text() != content:
            changed.append(target_path)
    return changed


def _render_maps_report(
    root: Path,
    target_paths: list[Path],
    changed_paths: list[Path],
    dry_run: bool,
    check: bool,
    current_date: date,
) -> str:
    mode = "check" if check else "dry-run" if dry_run else "apply"
    lines = [
        f"# Navigation Map Build Report - {current_date.isoformat()}",
        "",
        "## Summary",
        "",
        f"- Mode: {mode}",
        f"- Navigation maps: {len(target_paths)}",
        f"- Changed maps: {len(changed_paths)}",
        "",
        "## Map Pages",
        "",
    ]
    lines.extend(f"- `{display_path(path, root)}`" for path in target_paths)
    lines.extend(["", "## Changed Pages", ""])
    lines.extend(f"- `{display_path(path, root)}`" for path in changed_paths) if changed_paths else lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def _navigable_pages(root: Path, pages: list[Page]) -> list[Page]:
    excluded = {
        "wiki/index.md",
        "wiki/log.md",
        "wiki/inbox.md",
        "wiki/maps/README.md",
    }
    return [
        page
        for page in pages
        if display_path(page.path, root) not in excluded
    ]


def _page_lines(root: Path, pages: list[Page]) -> list[str]:
    if not pages:
        return ["- None."]
    lines = []
    for page in sorted(pages, key=lambda item: _wiki_target(root, item.path)):
        lines.append(f"- [[{_wiki_target(root, page.path)}]] — {_page_summary(page)}")
    return lines


def _page_type(page: Page) -> str:
    return str((page.frontmatter or {}).get("type", ""))


def _quality_value(page: Page, key: str) -> Any:
    quality = (page.frontmatter or {}).get("quality", {})
    if not isinstance(quality, dict):
        return None
    return quality.get(key)


def _page_summary(page: Page) -> str:
    return str((page.frontmatter or {}).get("summary", "No summary."))


def _wiki_target(root: Path, path: Path) -> str:
    return path.relative_to(root / "wiki").with_suffix("").as_posix()


def _source_page_link(source_page: str | None) -> str:
    if not source_page:
        return "none"
    path = Path(source_page)
    try:
        target = path.relative_to("wiki").with_suffix("").as_posix()
    except ValueError:
        return f"`{source_page}`"
    return f"[[{target}]]"


def _related_page_link(ref: str) -> str:
    path = Path(ref)
    try:
        target = path.relative_to("wiki").with_suffix("").as_posix()
    except ValueError:
        return f"`{ref}`"
    return f"[[{target}]]"


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]
