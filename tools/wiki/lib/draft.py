from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .frontmatter import Page, parse_page, render_page
from .lint import (
    ALLOWED_LOG_EVENTS,
    find_human_locks,
    validate_human_locks,
    validate_lifecycle,
    validate_log,
    validate_page_links,
    validate_provenance,
    validate_source_hash,
)
from .paths import resolve_report_path
from .schema import Issue, validate_schema
from .wiki_update import (
    append_log_entry,
    apply_index_entry,
    assert_can_append_log_entry,
    assert_can_apply_index_entry,
)


@dataclass(frozen=True)
class DraftResult:
    target_path: Path
    report_path: Path | None


@dataclass(frozen=True)
class BatchDraftResult:
    target_paths: list[Path]
    report_path: Path | None


def apply_draft(root: Path, draft_path: Path, report_path: Path | None = None, dry_run: bool = False) -> DraftResult:
    draft = _load_draft(draft_path)
    target_path = _resolve_wiki_target(root, draft)
    payload_errors = _validate_draft_payload(root, draft, target_path)
    if payload_errors:
        rendered = "\n".join(payload_errors)
        raise ValueError(f"Draft failed validation:\n{rendered}")
    page = Page(
        path=target_path,
        frontmatter=draft["frontmatter"],
        body=str(draft["body"]),
    )
    issues = _validate_draft_page(page, root / "wiki", root)
    if issues:
        rendered = "\n".join(_format_issue(issue) for issue in issues)
        raise ValueError(f"Draft failed validation:\n{rendered}")

    if target_path.exists():
        existing_page = parse_page(target_path)
        if find_human_locks(existing_page).page_locked:
            raise ValueError(f"Target page is human-locked: {target_path}")
        _validate_locked_sections_preserved(existing_page.body, page.body, target_path)

    if not dry_run:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(render_page(draft["frontmatter"], str(draft["body"])))
        apply_index_entry(root / "wiki" / "index.md", draft.get("index"))
        append_log_entry(root / "wiki" / "log.md", draft.get("log"))
        if report_path is not None:
            report_path = resolve_report_path(root, report_path)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(_render_report(draft_path, target_path))

    return DraftResult(target_path=target_path, report_path=report_path)


def apply_batch_draft(
    root: Path,
    draft_path: Path,
    report_path: Path | None = None,
    dry_run: bool = False,
) -> BatchDraftResult:
    batch = _load_batch_draft(draft_path)
    errors: list[str] = []
    page_payloads: list[tuple[dict[str, Any], Path]] = []
    seen_targets: set[Path] = set()

    _validate_log_entry(errors, batch.get("log"))
    _validate_side_effects(errors, root, None, batch.get("log"))
    for index, page_draft in enumerate(batch["pages"]):
        page_errors, target_path = _validate_batch_page(root, page_draft)
        errors.extend(f"[page {index}] {error}" for error in page_errors)
        if target_path is None:
            continue
        if target_path in seen_targets:
            errors.append(f"[page {index}] [error] duplicate-batch-target - Duplicate target: {target_path}.")
            continue
        seen_targets.add(target_path)
        if not page_errors:
            page_payloads.append((page_draft, target_path))

    if errors:
        raise ValueError("Batch draft failed validation:\n" + "\n".join(errors))

    target_paths = [target_path for _page_draft, target_path in page_payloads]
    if not dry_run:
        for page_draft, target_path in page_payloads:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(render_page(page_draft["frontmatter"], str(page_draft["body"])))
            apply_index_entry(root / "wiki" / "index.md", page_draft.get("index"))
        append_log_entry(root / "wiki" / "log.md", batch.get("log"))
        if report_path is not None:
            report_path = resolve_report_path(root, report_path)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(_render_batch_report(draft_path, target_paths))

    return BatchDraftResult(target_paths=target_paths, report_path=report_path)


def resolve_draft_target(root: Path, draft_path: Path) -> Path:
    return _resolve_wiki_target(root, _load_draft(draft_path))


def _validate_draft_payload(root: Path, draft: dict[str, Any], target_path: Path) -> list[str]:
    errors: list[str] = []
    _validate_index_entry(errors, root, draft.get("index"), target_path)
    _validate_log_entry(errors, draft.get("log"))
    _validate_side_effects(errors, root, draft.get("index"), draft.get("log"))
    return errors


def _validate_side_effects(errors: list[str], root: Path, index_entry: Any, log_entry: Any) -> None:
    checks = [
        (assert_can_apply_index_entry, root / "wiki" / "index.md", index_entry),
        (assert_can_append_log_entry, root / "wiki" / "log.md", log_entry),
    ]
    for check, path, entry in checks:
        try:
            check(path, entry)
        except ValueError as exc:
            errors.append(f"[error] human-lock-side-effect - {exc}")


def _validate_index_entry(errors: list[str], root: Path, index_entry: Any, target_path: Path) -> None:
    if index_entry is None:
        return
    if not isinstance(index_entry, dict):
        errors.append("[error] invalid-draft-index - Draft index must be an object.")
        return

    for field in ("section", "target", "summary"):
        if not isinstance(index_entry.get(field), str) or not index_entry[field].strip():
            errors.append(f"[error] invalid-draft-index - Draft index.{field} must be a non-empty string.")

    target = index_entry.get("target")
    if isinstance(target, str) and target.strip():
        expected_target = target_path.relative_to(root / "wiki").with_suffix("").as_posix()
        if target != expected_target:
            errors.append(
                "[error] invalid-draft-index - "
                f"Draft index.target must match target page '{expected_target}'."
            )


def _validate_log_entry(errors: list[str], log_entry: Any) -> None:
    if log_entry is None:
        return
    if not isinstance(log_entry, dict):
        errors.append("[error] invalid-draft-log - Draft log must be an object.")
        return

    event_type = log_entry.get("event_type")
    if not isinstance(event_type, str) or not event_type.strip():
        errors.append("[error] invalid-draft-log - Draft log.event_type must be a non-empty string.")
    elif event_type not in ALLOWED_LOG_EVENTS:
        errors.append(f"[error] invalid-draft-log - Invalid draft log.event_type: {event_type}.")

    if not isinstance(log_entry.get("title"), str) or not log_entry["title"].strip():
        errors.append("[error] invalid-draft-log - Draft log.title must be a non-empty string.")

    items = log_entry.get("items", [])
    if not isinstance(items, list):
        errors.append("[error] invalid-draft-log - Draft log.items must be a list when present.")
        return
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"[error] invalid-draft-log - Draft log.items[{index}] must be a non-empty string.")


def _load_draft(path: Path) -> dict[str, Any]:
    draft = json.loads(path.read_text())
    _validate_single_draft_shape(draft)
    return draft


def _validate_single_draft_shape(draft: dict[str, Any]) -> None:
    if draft.get("version") != 1:
        raise ValueError("Draft version must be 1.")
    if draft.get("operation") != "upsert-page":
        raise ValueError("Draft operation must be upsert-page.")
    if not isinstance(draft.get("frontmatter"), dict):
        raise ValueError("Draft frontmatter must be an object.")
    if not isinstance(draft.get("body"), str):
        raise ValueError("Draft body must be a string.")


def _load_batch_draft(path: Path) -> dict[str, Any]:
    batch = json.loads(path.read_text())
    if batch.get("version") != 1:
        raise ValueError("Batch draft version must be 1.")
    if batch.get("operation") != "batch-upsert-pages":
        raise ValueError("Batch draft operation must be batch-upsert-pages.")
    pages = batch.get("pages")
    if not isinstance(pages, list):
        raise ValueError("Batch draft pages must be a list.")
    for index, page_draft in enumerate(pages):
        if not isinstance(page_draft, dict):
            raise ValueError(f"Batch draft pages[{index}] must be an object.")
    return batch


def _validate_batch_page(root: Path, draft: dict[str, Any]) -> tuple[list[str], Path | None]:
    errors: list[str] = []
    try:
        _validate_single_draft_shape(draft)
        target_path = _resolve_wiki_target(root, draft)
    except ValueError as exc:
        return [str(exc)], None

    if draft.get("log") is not None:
        errors.append("[error] invalid-batch-page-log - Batch page drafts must not include log; use batch log.")
    draft_without_page_log = dict(draft)
    draft_without_page_log.pop("log", None)
    errors.extend(_validate_draft_payload(root, draft_without_page_log, target_path))
    page = Page(
        path=target_path,
        frontmatter=draft["frontmatter"],
        body=str(draft["body"]),
    )
    errors.extend(_format_issue(issue) for issue in _validate_draft_page(page, root / "wiki", root))

    if target_path.exists():
        existing_page = parse_page(target_path)
        if find_human_locks(existing_page).page_locked:
            errors.append(f"Target page is human-locked: {target_path}")
        try:
            _validate_locked_sections_preserved(existing_page.body, page.body, target_path)
        except ValueError as exc:
            errors.append(str(exc))
    return errors, target_path


def _resolve_wiki_target(root: Path, draft: dict[str, Any]) -> Path:
    raw_path = draft.get("path")
    if not isinstance(raw_path, str):
        raise ValueError("Draft path must be a string.")
    target_path = root / raw_path
    wiki_root = (root / "wiki").resolve()
    try:
        target_path.resolve().relative_to(wiki_root)
    except ValueError:
        raise ValueError("Draft path must stay under wiki/.") from None
    if target_path.suffix != ".md":
        raise ValueError("Draft path must target a markdown file.")
    return target_path


def _validate_draft_page(page: Page, wiki_root: Path, repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    issues.extend(validate_schema(page))
    issues.extend(validate_provenance(page, repo_root))
    issues.extend(validate_source_hash(page, repo_root))
    issues.extend(validate_lifecycle(page, wiki_root))
    issues.extend(validate_human_locks(page))
    if page.path.name == "log.md":
        issues.extend(validate_log(page))
    issues.extend(validate_page_links(page, wiki_root))
    return issues


def _validate_locked_sections_preserved(existing_body: str, proposed_body: str, target_path: Path) -> None:
    existing_sections = _locked_section_blocks(existing_body)
    if not existing_sections:
        return
    proposed_sections = _locked_section_blocks(proposed_body)
    if existing_sections != proposed_sections:
        raise ValueError(f"Draft modifies a human-locked section: {target_path}")


def _locked_section_blocks(body: str) -> list[str]:
    lines = body.splitlines()
    sections: list[str] = []
    stack: list[int] = []
    for index, line in enumerate(lines):
        if "<!-- human-locked:start -->" in line:
            stack.append(index)
        if "<!-- human-locked:end -->" in line and stack:
            start = stack.pop()
            sections.append("\n".join(lines[start : index + 1]))
    return sections


def _render_report(draft_path: Path, target_path: Path) -> str:
    return "\n".join(
        [
            f"# Draft Apply Report - {date.today().isoformat()}",
            "",
            "## Summary",
            "",
            f"- Applied draft: `{draft_path}`",
            f"- Target page: `{target_path}`",
            "",
        ]
    )


def _render_batch_report(draft_path: Path, target_paths: list[Path]) -> str:
    lines = [
        f"# Batch Draft Apply Report - {date.today().isoformat()}",
        "",
        "## Summary",
        "",
        f"- Applied batch draft: `{draft_path}`",
        f"- Target pages: {len(target_paths)}",
        "",
        "## Manifest",
        "",
    ]
    lines.extend(f"- `{target_path}`" for target_path in target_paths)
    lines.append("")
    return "\n".join(lines)


def _format_issue(issue: Issue) -> str:
    location = f":{issue.line}" if issue.line is not None else ""
    return f"[{issue.severity}] {issue.code} {issue.path}{location} - {issue.message}"
