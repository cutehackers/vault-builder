from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .frontmatter import parse_page, render_page
from .hash_source import compute_sha256, display_path
from .lint import find_human_locks
from .paths import resolve_report_path
from .session_archive import session_archive_root
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
    if _is_session_archive_path(root, source_path):
        raise ValueError(
            f"Session files do not get source pages: {display_path(source_path, root)}. "
            "Cite raw/sessions/ paths directly as claim evidence."
        )
    if not source_path.exists() or not source_path.is_file():
        raise ValueError(f"Source file does not exist: {source_path}")

    source_hash = compute_sha256(source_path)
    source_title = title or _title_from_path(source_path)
    source_summary = summary or f"Registered source file `{display_path(source_path, root)}`."
    if "\n" in source_summary or len(source_summary) > 240:
        raise ValueError("Source summary must be one line of at most 240 characters.")
    slug = _slugify(source_path.stem)
    source_page = root / "wiki" / "sources" / f"{slug}.md"
    canonical_source = display_path(source_path, root)
    source_kind = _source_kind(source_path)

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
        "source_kind": source_kind,
        "source_date": "",
        "authors": [],
        "url": "",
        "raw_sha256": source_hash,
        "quality": {
            "provenance": "section",
            "links": "unchecked",
            "contradictions": "unchecked",
            "review_required": False,
        },
    }
    body = _source_body(source_title, source_summary, canonical_source, source_hash, source_kind)
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


def _source_body(title: str, summary: str, canonical_source: str, source_hash: str, source_kind: str) -> str:
    return "\n".join(
        [
            f"# {title}",
            "",
            "## Summary",
            "",
            summary,
            "",
            "Source: `" + canonical_source + "`",
            "",
            "## Registration",
            "",
            "- Canonical source: `" + canonical_source + "`",
            "- SHA-256: `" + source_hash + "`",
            "- Source kind: `" + source_kind + "`",
            "",
            "## Durable Knowledge Links",
            "",
            "- None.",
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


def _is_session_archive_path(root: Path, source_path: Path) -> bool:
    try:
        source_path.resolve().relative_to(session_archive_root(root).resolve())
    except ValueError:
        return False
    return True


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


# import-source (vault-graph request S8, spec 4.4.5)

IMPORTS_DIR = "raw/imports"
_IMPORT_INTO_RE = re.compile(r"^raw/imports/(?P<kind>[a-z0-9][a-z0-9-]*)/?$")
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class ImportResult:
    status: str  # "imported" or "exists"
    raw_path: str
    source_page: str
    sha256: str
    created: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.status,
            "raw_path": self.raw_path,
            "source_page": self.source_page,
            "sha256": self.sha256,
            "created": list(self.created),
        }


def import_source(root: Path, file_path: Path, into: str) -> ImportResult:
    """Copy a file from outside raw/ to raw/imports/<kind>/<stem>--<sha256 first 8><suffix> and ingest it.

    When a source page already records the file's sha256, nothing is written and that page's
    canonical_source and path are returned. Raises ValueError and writes nothing on bad input.
    """
    match = _IMPORT_INTO_RE.match(into)
    if match is None:
        raise ValueError(
            f"--into must be {IMPORTS_DIR}/<kind>/ where kind uses lowercase letters, digits, and '-': {into!r}."
        )
    source = Path(file_path).expanduser()
    if not source.is_file():
        raise ValueError(f"File does not exist or is not a regular file: {source.name}.")
    resolved = source.resolve()
    if _is_under(resolved, (root / "raw").resolve()):
        raise ValueError(
            f"{display_path(resolved, root)} is already under raw/; run ingest-source on it instead of import-source."
        )
    data = resolved.read_bytes()
    digest = hashlib.sha256(data).hexdigest()

    existing = _source_page_with_hash(root, digest)
    if existing is not None:
        page, canonical = existing
        return ImportResult("exists", canonical, page, digest, ())

    stem = _UNSAFE_NAME_CHARS.sub("-", source.stem).strip("-.") or "file"
    suffix = _UNSAFE_NAME_CHARS.sub("", source.suffix)
    target = root / IMPORTS_DIR / match.group("kind") / f"{stem}--{digest[:8]}{suffix}"
    raw_relative = display_path(target, root)
    created: list[str] = []
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError(f"{raw_relative} already exists with other bytes; nothing written.")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        created.append(raw_relative)
    try:
        result = ingest_source(root, target, summary=f"Imported with import-source from {source.name}."[:240])
    except Exception:
        if created:
            target.unlink()
        raise
    page_relative = display_path(result.source_page, root)
    if result.outcome != "unchanged":
        created.append(page_relative)
    return ImportResult("imported", raw_relative, page_relative, digest, tuple(created))


def _source_page_with_hash(root: Path, digest: str) -> tuple[str, str] | None:
    sources = root / "wiki" / "sources"
    if not sources.is_dir():
        return None
    for page_path in sorted(sources.glob("*.md")):
        try:
            frontmatter = parse_page(page_path).frontmatter or {}
        except (OSError, UnicodeDecodeError):
            continue
        canonical = frontmatter.get("canonical_source")
        if frontmatter.get("raw_sha256") == digest and isinstance(canonical, str) and canonical:
            return display_path(page_path, root), canonical
    return None


def _is_under(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
    except ValueError:
        return False
    return True
