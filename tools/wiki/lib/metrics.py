from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .frontmatter import Page, parse_page
from .hash_source import display_path
from .links import find_wiki_links
from .lint import lint_repo
from .paths import resolve_report_path
from .review import list_review_items
from .source_registry import build_source_registry


SUBSTANTIVE_TYPES = {
    "overview",
    "concept",
    "entity",
    "system",
    "workflow",
    "decision",
    "claim",
    "comparison",
    "timeline",
    "question",
    "glossary",
}


@dataclass(frozen=True)
class MaintenanceMetrics:
    pending_reviews: int
    unresolved_review_max_age_days: int
    contested_claim_rows: int
    unreviewed_contested_claim_rows: int
    stale_sources: int
    unregistered_sources: int
    orphan_pages: int
    pages_without_claim_level_provenance: int
    deprecated_linked_pages: int
    deprecated_linked_targets: list[str]
    lint_errors: int
    last_health_report: str | None


@dataclass(frozen=True)
class MaintenanceMetricsResult:
    metrics: MaintenanceMetrics
    report_path: Path | None


def build_maintenance_metrics(
    root: Path,
    report_path: Path | None = None,
    metrics_date: date | None = None,
) -> MaintenanceMetricsResult:
    current_date = metrics_date or date.today()
    pages = _load_wiki_pages(root / "wiki")
    lint_result = lint_repo(root)
    source_entries = build_source_registry(root, checked_at=current_date)
    review_items = list_review_items(root)

    pending_reviews = [item for item in review_items if item.status == "pending"]
    contested_claim_rows = sum(_claim_status_counts(page).get("contested", 0) for page in pages)
    pending_contested_reviews = sum(1 for item in pending_reviews if item.review_type == "contested-claim")
    stale_sources = sum(1 for entry in source_entries if entry.ingest_status in {"drift", "missing-source"})
    unregistered_sources = sum(1 for entry in source_entries if entry.ingest_status == "unregistered")
    orphan_pages = sum(1 for issue in lint_result.issues if issue.code == "orphan-page")
    pages_without_claim_level_provenance = sum(1 for page in pages if _is_substantive_without_claim_provenance(page))
    deprecated_linked_targets = _deprecated_linked_targets(root, pages)

    metrics = MaintenanceMetrics(
        pending_reviews=len(pending_reviews),
        unresolved_review_max_age_days=_max_pending_review_age_days(pending_reviews, current_date),
        contested_claim_rows=contested_claim_rows,
        unreviewed_contested_claim_rows=max(contested_claim_rows - pending_contested_reviews, 0),
        stale_sources=stale_sources,
        unregistered_sources=unregistered_sources,
        orphan_pages=orphan_pages,
        pages_without_claim_level_provenance=pages_without_claim_level_provenance,
        deprecated_linked_pages=len(deprecated_linked_targets),
        deprecated_linked_targets=deprecated_linked_targets,
        lint_errors=sum(1 for issue in lint_result.issues if issue.severity == "error"),
        last_health_report=_latest_health_report(root),
    )

    if report_path is not None:
        report_path = resolve_report_path(root, report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_maintenance_metrics_report(metrics, current_date))

    return MaintenanceMetricsResult(metrics=metrics, report_path=report_path)


def render_maintenance_metrics_report(metrics: MaintenanceMetrics, report_date: date | None = None) -> str:
    current_date = report_date or date.today()
    lines = [
        f"# Maintenance Metrics Report - {current_date.isoformat()}",
        "",
        "## Summary",
        "",
        f"- Pending reviews: {metrics.pending_reviews}",
        f"- Unresolved review max age days: {metrics.unresolved_review_max_age_days}",
        f"- Contested claim rows: {metrics.contested_claim_rows}",
        f"- Unreviewed contested claim rows: {metrics.unreviewed_contested_claim_rows}",
        f"- Stale sources: {metrics.stale_sources}",
        f"- Unregistered sources: {metrics.unregistered_sources}",
        f"- Orphan pages: {metrics.orphan_pages}",
        f"- Pages without claim-level provenance: {metrics.pages_without_claim_level_provenance}",
        f"- Deprecated linked pages: {metrics.deprecated_linked_pages}",
        f"- Lint errors: {metrics.lint_errors}",
        f"- Last health report: `{metrics.last_health_report}`" if metrics.last_health_report else "- Last health report: none",
        "",
        "## Deprecated Linked Pages",
        "",
    ]
    if metrics.deprecated_linked_targets:
        lines.extend(f"- [[{target}]]" for target in metrics.deprecated_linked_targets)
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def _load_wiki_pages(wiki_root: Path) -> list[Page]:
    if not wiki_root.exists():
        return []
    pages: list[Page] = []
    for path in sorted(wiki_root.rglob("*.md")):
        page = parse_page(path)
        if page.frontmatter is not None:
            pages.append(page)
    return pages


def _claim_status_counts(page: Page) -> Counter[str]:
    counts: Counter[str] = Counter()
    in_claim_table = False
    for line in page.body.splitlines():
        cells = _table_cells(line)
        if not cells:
            in_claim_table = False
            continue
        normalized = [cell.lower() for cell in cells]
        if normalized[:3] == ["claim", "status", "evidence"]:
            in_claim_table = True
            continue
        if in_claim_table and set(cells) <= {"---", ":---", "---:", ":---:"}:
            continue
        if in_claim_table and len(cells) >= 3:
            counts[cells[1].strip().lower()] += 1
    return counts


def _table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _is_substantive_without_claim_provenance(page: Page) -> bool:
    frontmatter = page.frontmatter or {}
    if str(frontmatter.get("type")) not in SUBSTANTIVE_TYPES:
        return False
    quality = frontmatter.get("quality", {})
    if not isinstance(quality, dict):
        return True
    return quality.get("provenance") != "claim"


def _deprecated_linked_targets(root: Path, pages: list[Page]) -> list[str]:
    deprecated_targets = {
        _wiki_target(root, page.path)
        for page in pages
        if str((page.frontmatter or {}).get("status")) == "deprecated"
    }
    linked_targets = Counter[str]()
    for page in pages:
        for target in find_wiki_links(page.body):
            linked_targets[target] += 1
    return sorted(target for target in deprecated_targets if linked_targets[target] > 0)


def _wiki_target(root: Path, path: Path) -> str:
    return path.relative_to(root / "wiki").with_suffix("").as_posix()


def _max_pending_review_age_days(review_items: list[Any], current_date: date) -> int:
    ages = []
    for item in review_items:
        try:
            page = parse_page(item.path)
            created = date.fromisoformat(str((page.frontmatter or {}).get("created")))
        except (OSError, ValueError):
            continue
        ages.append((current_date - created).days)
    return max(ages, default=0)


def _latest_health_report(root: Path) -> str | None:
    reports_root = root / "scratch" / "reports"
    if not reports_root.exists():
        return None
    reports = sorted(reports_root.glob("*-lint.md"))
    if not reports:
        return None
    return display_path(reports[-1], root)
