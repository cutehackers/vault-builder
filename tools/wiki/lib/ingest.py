from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .frontmatter import parse_page, render_page
from .hash_source import compute_sha256, display_path
from .lint import find_human_locks
from .paths import resolve_report_path
from .wiki_update import append_log_entry, apply_index_entry, assert_can_append_log_entry, assert_can_apply_index_entry


@dataclass(frozen=True)
class IngestResult:
    source_page: Path
    report_path: Path | None
    raw_hash: str
    outcome: str


def ingest_source(
    root: Path,
    source_path: Path,
    summary: str | None = None,
    title: str | None = None,
    report_path: Path | None = None,
) -> IngestResult:
    if report_path is not None:
        report_path = resolve_report_path(root, report_path)

    if not source_path.is_absolute():
        source_path = root / source_path
    raw_root = (root / "raw").resolve()
    try:
        source_path.resolve().relative_to(raw_root)
    except ValueError:
        raise ValueError("Source path must be under raw/.") from None
    if not source_path.exists() or not source_path.is_file():
        raise ValueError(f"Source file does not exist: {source_path}")

    source_hash = compute_sha256(source_path)
    source_title = title or _title_from_path(source_path)
    source_summary = summary or f"Registered source file `{display_path(source_path, root)}`."
    slug = _slugify(source_path.stem)
    source_page = root / "wiki" / "sources" / f"{slug}.md"
    canonical_source = display_path(source_path, root)

    if source_page.exists():
        existing_page = parse_page(source_page)
        if find_human_locks(existing_page).page_locked:
            raise ValueError(f"human-locked source page: {display_path(source_page, root)}")
        if not existing_page.frontmatter:
            raise ValueError(f"invalid source page frontmatter: {display_path(source_page, root)}")
        existing_canonical_source = existing_page.frontmatter.get("canonical_source")
        existing_hash = existing_page.frontmatter.get("raw_sha256")
        if existing_canonical_source == canonical_source:
            if existing_hash == source_hash:
                if report_path is not None:
                    _write_report(root, report_path, source_page, canonical_source, source_hash, "unchanged")
                return IngestResult(
                    source_page=source_page,
                    report_path=report_path,
                    raw_hash=source_hash,
                    outcome="unchanged",
                )
            _write_report(root, report_path, source_page, canonical_source, source_hash, "drift")
            raise ValueError(
                "source-drift: existing source page points to the same canonical_source "
                f"but raw_sha256 differs: {display_path(source_page, root)}"
            )
        _write_report(root, report_path, source_page, canonical_source, source_hash, "collision")
        raise ValueError(
            "slug-collision: existing source page uses this slug for another canonical_source: "
            f"{display_path(source_page, root)}"
        )

    frontmatter = {
        "title": source_title,
        "type": "source",
        "status": "active",
        "created": date.today().isoformat(),
        "updated": date.today().isoformat(),
        "owner": "agent",
        "summary": source_summary,
        "source_count": 1,
        "tags": ["source"],
        "related": [],
        "confidence": "high",
        "canonical_source": canonical_source,
        "source_kind": _source_kind(source_path),
        "source_date": "",
        "authors": [],
        "url": "",
        "raw_sha256": source_hash,
        "quality": {
            "provenance": "claim",
            "links": "unchecked",
            "contradictions": "unchecked",
            "review_required": False,
        },
    }
    body = _source_body(source_title, source_summary, canonical_source)
    target = f"sources/{slug}"
    index_entry = {
        "section": "Sources",
        "target": target,
        "summary": source_summary,
    }
    log_entry = {
        "event_type": "ingest",
        "title": source_title,
        "items": [
            f"Added source page: [[{target}]].",
            f"Canonical source: `{canonical_source}`.",
            "Contradictions found: unchecked.",
        ],
    }
    assert_can_apply_index_entry(root / "wiki" / "index.md", index_entry)
    assert_can_append_log_entry(root / "wiki" / "log.md", log_entry)

    source_page.parent.mkdir(parents=True, exist_ok=True)
    source_page.write_text(render_page(frontmatter, body))
    apply_index_entry(root / "wiki" / "index.md", index_entry)
    append_log_entry(root / "wiki" / "log.md", log_entry)

    if report_path is not None:
        _write_report(root, report_path, source_page, canonical_source, source_hash, "created")

    return IngestResult(source_page=source_page, report_path=report_path, raw_hash=source_hash, outcome="created")


def _write_report(
    root: Path,
    report_path: Path | None,
    source_page: Path,
    canonical_source: str,
    source_hash: str,
    outcome: str,
) -> None:
    if report_path is None:
        return
    report_path = resolve_report_path(root, report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_render_report(source_page, canonical_source, source_hash, outcome))


def _source_body(title: str, summary: str, canonical_source: str) -> str:
    return "\n".join(
        [
            f"# {title}",
            "",
            "## Summary",
            "",
            summary,
            "",
            "Evidence: `" + canonical_source + "`",
            "",
            "## Key Takeaways",
            "",
            "- Not extracted yet.",
            "",
            "## Extracted Concepts",
            "",
            "- None yet.",
            "",
            "## Important Claims",
            "",
            "| Claim | Status | Evidence |",
            "|---|---|---|",
            "| Source registered for future extraction. | stated | `" + canonical_source + "` |",
            "",
            "## Contradictions / Tensions",
            "",
            "- Unchecked.",
            "",
            "## Pages Updated During Ingest",
            "",
            "- This source page.",
            "",
            "## Follow-up Questions",
            "",
            "- Which durable concepts should be extracted from this source?",
        ]
    )


def _render_report(source_page: Path, canonical_source: str, source_hash: str, outcome: str) -> str:
    applied_fixes = [
        "- Created or updated source page.",
        "- Updated wiki index.",
        "- Appended wiki log.",
    ]
    if outcome == "unchanged":
        applied_fixes = [
            "- No durable changes.",
            "- Existing source page already matches canonical_source and raw_sha256.",
        ]
    elif outcome in {"drift", "collision"}:
        applied_fixes = [
            "- No durable changes.",
            "- Existing source page was preserved.",
        ]
    return "\n".join(
        [
            f"# Ingest Report - {date.today().isoformat()}",
            "",
            "## Summary",
            "",
            f"- Outcome: {outcome}",
            f"- Ingested source: `{canonical_source}`",
            f"- Source page: `{source_page}`",
            f"- SHA-256: `{source_hash}`",
            "",
            "## Applied Fixes",
            "",
            *applied_fixes,
            "",
        ]
    )


def _title_from_path(path: Path) -> str:
    return path.stem.replace("-", " ").replace("_", " ").title()


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "source"


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
