from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .frontmatter import Page, parse_page, render_page
from .hash_source import display_path
from .inbox import InboxItem, load_inbox_items
from .lint import find_human_locks, project_ids
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


MAP_GENERATED_BY = "tools/wiki/cli.py maps build"
PROJECT_SECTION_MAX_TOKENS = 750
PROJECT_MAP_MAX_TOKENS = 1500
PROJECT_SECTION_HEADINGS = ("## Open Work", "## Current Decisions", "## Corrections and Rules")
PROJECT_CORRECTION_TAGS = frozenset({"correction", "rule"})


def estimate_tokens(text: str) -> int:
    """Return estimated tokens: Hangul characters / 2 + all other characters / 4, rounded up.

    Characters are Unicode code points, spaces and newlines included. Hangul characters are the code points
    in U+1100-U+11FF, U+3130-U+318F, U+A960-U+A97F, U+AC00-U+D7A3, and U+D7B0-U+D7FF.
    """
    hangul = sum(1 for char in text if _is_hangul(char))
    other = len(text) - hangul
    return (2 * hangul + other + 3) // 4


def _is_hangul(char: str) -> bool:
    code = ord(char)
    return (
        0x1100 <= code <= 0x11FF
        or 0x3130 <= code <= 0x318F
        or 0xA960 <= code <= 0xA97F
        or 0xAC00 <= code <= 0xD7A3
        or 0xD7B0 <= code <= 0xD7FF
    )


@dataclass(frozen=True)
class NavigationMapBuildResult:
    target_paths: list[Path]
    changed_paths: list[Path]
    report_path: Path | None
    dry_run: bool
    check: bool
    deleted_paths: list[Path] = field(default_factory=list)


def build_navigation_maps(
    root: Path,
    report_path: Path | None = None,
    dry_run: bool = False,
    check: bool = False,
    build_date: date | None = None,
) -> NavigationMapBuildResult:
    current_date = build_date or date.today()
    wiki_root = root / "wiki"
    project_dir = _project_map_dir(root)
    pages = [page for page in _load_wiki_pages(wiki_root) if page.path.parent != project_dir]
    source_entries = build_source_registry(root, checked_at=current_date)
    review_items = list_review_items(root)
    project_pages = _project_summary_pages(root, pages, load_inbox_items(root), current_date)
    deleted_paths = _stale_project_map_paths(root, project_pages)

    rendered_pages = _render_map_pages(root, pages, source_entries, review_items, current_date, project_pages)
    target_paths = [path for path, _content in rendered_pages]
    written_paths = _changed_paths(rendered_pages)
    changed_paths = written_paths + deleted_paths
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
    # Project summaries can be removed later, so log their path instead of a wiki link that would break.
    log_items = [
        f"Updated `{display_path(path, root)}`." if path.parent == project_dir else f"Updated [[maps/{path.stem}]]."
        for path in written_paths
    ]
    log_items.extend(
        f"Removed `{display_path(path, root)}`: project `{path.stem}` has no pages or open work."
        for path in deleted_paths
    )
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
        for path in deleted_paths:
            path.unlink()
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
        report_path.write_text(
            _render_maps_report(root, target_paths, changed_paths, dry_run, check, current_date, deleted_paths)
        )

    return NavigationMapBuildResult(
        target_paths=target_paths,
        changed_paths=changed_paths,
        report_path=report_path,
        dry_run=dry_run,
        check=check,
        deleted_paths=deleted_paths,
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
    project_pages: list[Page] | None = None,
) -> list[tuple[Path, str]]:
    wiki_root = root / "wiki"
    project_pages = project_pages or []
    topic_pages = _pages_with_generated_map_pages(root, pages, current_date, project_pages)
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
    for page in project_pages:
        rendered.append((page.path, _render_stable_map_page(page.path, page.frontmatter or {}, page.body)))
    return rendered


def _pages_with_generated_map_pages(
    root: Path,
    pages: list[Page],
    current_date: date,
    project_pages: list[Page] | None = None,
) -> list[Page]:
    wiki_root = root / "wiki"
    project_pages = project_pages or []
    generated_paths = {wiki_root / "maps" / f"{slug}.md" for slug in MAP_DEFINITIONS}
    generated_paths.update(page.path for page in project_pages)
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
    navigable_pages.extend(project_pages)
    return navigable_pages


def _project_map_dir(root: Path) -> Path:
    return root / "wiki" / "maps" / "projects"


def _project_summary_pages(
    root: Path,
    pages: list[Page],
    inbox_items: list[InboxItem],
    current_date: date,
) -> list[Page]:
    """Render one summary page per project id found in page frontmatter or open inbox items."""
    open_work: dict[str, list[InboxItem]] = {}
    decisions: dict[str, list[Page]] = {}
    corrections: dict[str, list[Page]] = {}
    ids: set[str] = set()
    for item in inbox_items:
        if not item.is_open:
            continue
        for project_id in project_ids(item.project):
            ids.add(project_id)
            open_work.setdefault(project_id, []).append(item)
    for page in pages:
        frontmatter = page.frontmatter or {}
        page_ids = project_ids(frontmatter.get("project"))
        ids.update(page_ids)
        is_decision = _page_type(page) == "decision" and str(frontmatter.get("status")) != "deprecated"
        is_correction = bool(PROJECT_CORRECTION_TAGS & set(_as_string_list(frontmatter.get("tags", []))))
        for project_id in page_ids:
            if is_decision:
                decisions.setdefault(project_id, []).append(page)
            if is_correction:
                corrections.setdefault(project_id, []).append(page)

    summaries: list[Page] = []
    for project_id in sorted(ids):
        target = _project_map_dir(root) / f"{project_id}.md"
        _assert_generated_project_map(root, target)
        open_items = sorted(
            sorted(open_work.get(project_id, []), key=lambda item: item.line),
            key=lambda item: item.date,
            reverse=True,
        )
        sections = [
            [f"- [[inbox]] — {item.title} ({item.status})" for item in open_items],
            _project_page_lines(root, decisions.get(project_id, [])),
            _project_page_lines(root, corrections.get(project_id, [])),
        ]
        summaries.append(
            Page(
                path=target,
                frontmatter=_project_frontmatter(project_id, target, current_date),
                body=_render_project_summary(project_id, sections),
            )
        )
    return summaries


def _project_page_lines(root: Path, pages: list[Page]) -> list[str]:
    ordered = sorted(
        sorted(pages, key=lambda page: display_path(page.path, root)),
        key=lambda page: str((page.frontmatter or {}).get("updated", "")),
        reverse=True,
    )
    return [
        f"- [[{_wiki_target(root, page.path)}]] — {_page_summary(page)} ({display_path(page.path, root)})"
        for page in ordered
    ]


def _project_frontmatter(project_id: str, target: Path, current_date: date) -> dict[str, Any]:
    definition = {
        "title": f"Project Summary: {project_id}",
        "summary": f"Generated summary of open work, current decisions, and corrections and rules for project {project_id}.",
        "tags": ["project-map"],
    }
    frontmatter = _map_frontmatter(f"projects/{project_id}", definition, target, current_date)
    frontmatter["project"] = project_id
    return frontmatter


def _render_project_summary(project_id: str, sections: list[list[str]]) -> str:
    """Render the fixed sections, dropping trailing lines until both token limits hold."""
    header = [
        f"# Project Summary: {project_id}",
        "",
        "## Purpose",
        "",
        "This generated page lists open work, current decisions, and corrections and rules for this project.",
        "",
        "Source: Deterministic map build from wiki frontmatter `project` values and `wiki/inbox.md` items.",
        "",
    ]
    kept = [list(lines) for lines in sections]
    omitted = [0 for _lines in sections]

    def section_block(index: int) -> list[str]:
        items = list(kept[index])
        if omitted[index]:
            items.append(f"- {omitted[index]} more items omitted by the size limit; see [[maps/topic-map]].")
        return [PROJECT_SECTION_HEADINGS[index], "", *(items or ["- (none)"]), ""]

    def section_tokens(index: int) -> int:
        return estimate_tokens("\n".join(section_block(index)) + "\n")

    def body() -> str:
        lines = list(header)
        for index in range(len(kept)):
            lines.extend(section_block(index))
        return "\n".join(lines).rstrip() + "\n"

    def drop_last(index: int) -> None:
        kept[index].pop()
        omitted[index] += 1

    for index in range(len(kept)):
        while kept[index] and section_tokens(index) > PROJECT_SECTION_MAX_TOKENS:
            drop_last(index)
    # The page body as parsed starts with the blank line that follows the frontmatter.
    while estimate_tokens("\n" + body()) > PROJECT_MAP_MAX_TOKENS:
        candidates = [index for index in range(len(kept)) if kept[index]]
        if not candidates:
            break
        drop_last(max(candidates, key=lambda index: (section_tokens(index), -index)))
    return body()


def _assert_generated_project_map(root: Path, path: Path) -> None:
    if not path.exists():
        return
    page = parse_page(path)
    if find_human_locks(page).page_locked:
        raise ValueError(f"Project summary page is human-locked: {display_path(path, root)}")
    if (page.frontmatter or {}).get("generated_by") != MAP_GENERATED_BY:
        raise ValueError(f"Project summary page was not generated by maps build: {display_path(path, root)}")


def _stale_project_map_paths(root: Path, project_pages: list[Page]) -> list[Path]:
    """Return generated summary pages whose project no longer has pages or open work."""
    project_dir = _project_map_dir(root)
    if not project_dir.is_dir():
        return []
    current = {page.path for page in project_pages}
    stale = [path for path in sorted(project_dir.glob("*.md")) if path not in current]
    for path in stale:
        _assert_generated_project_map(root, path)
    return stale


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
        "generated_by": MAP_GENERATED_BY,
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
    deleted_paths: list[Path] | None = None,
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
    if deleted_paths:
        lines.extend(["", "## Removed Project Summaries", ""])
        lines.extend(f"- `{display_path(path, root)}`" for path in deleted_paths)
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
