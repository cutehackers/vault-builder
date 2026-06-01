from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

from .frontmatter import parse_page
from .hash_source import compute_sha256, display_path
from .ingest import ingest_source
from .paths import resolve_report_path


@dataclass(frozen=True)
class SourceRegistryEntry:
    raw_path: str
    raw_sha256: str | None
    source_kind: str
    source_page: str | None
    ingest_status: str
    last_checked: str


@dataclass(frozen=True)
class BulkIngestItem:
    source_path: str
    source_page: str | None
    raw_hash: str | None
    outcome: str
    error: str = ""


@dataclass(frozen=True)
class BulkIngestResult:
    items: list[BulkIngestItem]
    report_path: Path | None

    @property
    def ok(self) -> bool:
        return all(item.outcome not in {"error", "drift", "collision"} for item in self.items)

    def counts_by_outcome(self) -> Counter[str]:
        return Counter(item.outcome for item in self.items)


def build_source_registry(root: Path, checked_at: date | None = None) -> list[SourceRegistryEntry]:
    current_date = (checked_at or date.today()).isoformat()
    source_pages = _source_pages_by_canonical(root)
    entries: dict[str, SourceRegistryEntry] = {}

    for raw_path in discover_raw_files(root):
        raw_display = display_path(raw_path, root)
        raw_hash = compute_sha256(raw_path)
        source_page = source_pages.get(raw_display)
        source_page_path = source_page.path if source_page else None
        expected_hash = source_page.raw_sha256 if source_page else None
        status = "unregistered"
        if source_page is not None:
            status = "registered" if expected_hash == raw_hash else "drift"
        entries[raw_display] = SourceRegistryEntry(
            raw_path=raw_display,
            raw_sha256=raw_hash,
            source_kind=_source_kind(raw_path),
            source_page=source_page_path,
            ingest_status=status,
            last_checked=current_date,
        )

    for canonical_source, source_page in source_pages.items():
        if canonical_source in entries:
            continue
        entries[canonical_source] = SourceRegistryEntry(
            raw_path=canonical_source,
            raw_sha256=None,
            source_kind=_source_kind(root / canonical_source),
            source_page=source_page.path,
            ingest_status="missing-source",
            last_checked=current_date,
        )

    return [entries[key] for key in sorted(entries)]


def discover_raw_files(root: Path) -> list[Path]:
    raw_root = root / "raw"
    if not raw_root.exists():
        return []
    return sorted(
        path
        for path in raw_root.rglob("*")
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(raw_root).parts)
    )


def render_source_registry_report(entries: list[SourceRegistryEntry], report_date: date | None = None) -> str:
    current_date = report_date or date.today()
    counts = Counter(entry.ingest_status for entry in entries)
    lines = [
        f"# Source Registry Report - {current_date.isoformat()}",
        "",
        "## Summary",
        "",
        f"- Entries: {len(entries)}",
        f"- Registered: {counts.get('registered', 0)}",
        f"- Unregistered: {counts.get('unregistered', 0)}",
        f"- Drift: {counts.get('drift', 0)}",
        f"- Missing Source: {counts.get('missing-source', 0)}",
        "",
        "## Entries",
        "",
        "| Raw Source | Status | Source Kind | Source Page | SHA-256 | Last Checked |",
        "|---|---|---|---|---|---|",
    ]
    for entry in entries:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{entry.raw_path}`",
                    entry.ingest_status,
                    entry.source_kind,
                    f"`{entry.source_page}`" if entry.source_page else "",
                    f"`{entry.raw_sha256}`" if entry.raw_sha256 else "",
                    entry.last_checked,
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def bulk_ingest_sources(
    root: Path,
    source_paths: Iterable[Path],
    report_path: Path | None = None,
) -> BulkIngestResult:
    snapshot = _snapshot_bulk_ingest_paths(root)
    items: list[BulkIngestItem] = []
    for source_path in source_paths:
        if not source_path.is_absolute():
            source_path = root / source_path
        try:
            result = ingest_source(root, source_path)
            items.append(
                BulkIngestItem(
                    source_path=display_path(source_path, root),
                    source_page=display_path(result.source_page, root),
                    raw_hash=result.raw_hash,
                    outcome=result.outcome,
                )
            )
        except ValueError as exc:
            items.append(
                BulkIngestItem(
                    source_path=display_path(source_path, root),
                    source_page=None,
                    raw_hash=None,
                    outcome=_outcome_from_error(str(exc)),
                    error=str(exc),
                )
            )

    if any(item.outcome in {"error", "drift", "collision"} for item in items):
        _restore_snapshot(root, snapshot)

    if report_path is not None:
        report_path = resolve_report_path(root, report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_bulk_ingest_report(items))

    return BulkIngestResult(items=items, report_path=report_path)


def render_bulk_ingest_report(items: list[BulkIngestItem], report_date: date | None = None) -> str:
    current_date = report_date or date.today()
    counts = Counter(item.outcome for item in items)
    lines = [
        f"# Bulk Ingest Report - {current_date.isoformat()}",
        "",
        "## Summary",
        "",
        f"- Total: {len(items)}",
        f"- Created: {counts.get('created', 0)}",
        f"- Unchanged: {counts.get('unchanged', 0)}",
        f"- Drift: {counts.get('drift', 0)}",
        f"- Collision: {counts.get('collision', 0)}",
        f"- Error: {counts.get('error', 0)}",
        "",
        "## Items",
        "",
        "| Source | Outcome | Source Page | SHA-256 | Error |",
        "|---|---|---|---|---|",
    ]
    for item in items:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{item.source_path}`",
                    item.outcome,
                    f"`{item.source_page}`" if item.source_page else "",
                    f"`{item.raw_hash}`" if item.raw_hash else "",
                    item.error.replace("|", "\\|"),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True)
class _SourcePage:
    path: str
    raw_sha256: str | None


def _source_pages_by_canonical(root: Path) -> dict[str, _SourcePage]:
    source_pages: dict[str, _SourcePage] = {}
    for path in sorted((root / "wiki" / "sources").glob("*.md")):
        page = parse_page(path)
        if not page.frontmatter or page.frontmatter.get("type") != "source":
            continue
        canonical_source = page.frontmatter.get("canonical_source")
        if not canonical_source:
            continue
        source_pages[str(canonical_source)] = _SourcePage(
            path=display_path(path, root),
            raw_sha256=page.frontmatter.get("raw_sha256"),
        )
    return source_pages


def _source_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "image"
    if suffix in {".py", ".js", ".ts", ".tsx", ".jsx", ".sh"}:
        return "code"
    return "other"


def _outcome_from_error(message: str) -> str:
    if message.startswith("source-drift"):
        return "drift"
    if message.startswith("slug-collision"):
        return "collision"
    return "error"


def _snapshot_bulk_ingest_paths(root: Path) -> dict[Path, str | None]:
    paths = set((root / "wiki" / "sources").glob("*.md"))
    paths.add(root / "wiki" / "index.md")
    paths.add(root / "wiki" / "log.md")
    return {path: path.read_text() if path.exists() else None for path in paths}


def _restore_snapshot(root: Path, snapshot: dict[Path, str | None]) -> None:
    snapshotted_paths = set(snapshot)
    for path in (root / "wiki" / "sources").glob("*.md"):
        if path not in snapshotted_paths:
            path.unlink(missing_ok=True)
    for path, content in snapshot.items():
        if content is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
