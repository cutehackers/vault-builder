from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from pathlib import Path

from .frontmatter import Page
from .schema import Issue


class PageRole(str, Enum):
    RAW_EVIDENCE = "raw-evidence"
    CANONICAL_KNOWLEDGE = "canonical-knowledge"
    SOURCE_MANIFEST = "source-manifest"
    GENERATED_VIEW = "generated-view"
    OPERATION_LOG = "operation-log"
    AUDIT_REPORT = "audit-report"
    OPERATING_CONTRACT = "operating-contract"


SOURCE_MANIFEST_LEVEL_TWO_HEADINGS = {
    "## Summary",
    "## Registration",
    "## Durable Knowledge Links",
}


def authority_contract_version(root: Path) -> int | None:
    path = root / ".vault-builder" / "manifest.json"
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_file() or path.is_symlink():
        raise ValueError("Vault builder manifest must be a regular file.")
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Vault builder manifest is invalid.") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Vault builder manifest must be a JSON object.")
    value = manifest.get("contract_version")
    if not isinstance(value, int):
        raise ValueError("Vault builder manifest contract_version must be an integer.")
    return value


def classify_page_role(root: Path, page: Page) -> PageRole:
    relative = page.path.resolve().relative_to(root.resolve())
    path = relative.as_posix()
    page_type = (page.frontmatter or {}).get("type")
    if path.startswith("raw/"):
        return PageRole.RAW_EVIDENCE
    if path.startswith("scratch/reports/"):
        return PageRole.AUDIT_REPORT
    if path == "wiki/log.md" or page_type == "log":
        return PageRole.OPERATION_LOG
    if path == "wiki/index.md" or path.startswith("wiki/maps/") or page_type in {"index", "map"}:
        return PageRole.GENERATED_VIEW
    if path.startswith("wiki/sources/") or page_type == "source":
        return PageRole.SOURCE_MANIFEST
    if path.startswith("docs/") or path == "AGENTS.md":
        return PageRole.OPERATING_CONTRACT
    return PageRole.CANONICAL_KNOWLEDGE


def validate_authority_contract(root: Path, pages: list[Page]) -> list[Issue]:
    try:
        version = authority_contract_version(root)
    except ValueError as exc:
        return [
            Issue(
                severity="error",
                code="invalid-vault-builder-manifest",
                path=str(root / ".vault-builder" / "manifest.json"),
                message=str(exc),
            )
        ]

    if version is None or version < 1:
        return []

    issues = [issue for page in pages for issue in validate_page_authority(root, page)]
    issues.extend(_validate_duplicate_canonical_bodies(root, pages))
    return issues


def validate_page_authority(root: Path, page: Page) -> list[Issue]:
    if classify_page_role(root, page) != PageRole.SOURCE_MANIFEST:
        return []

    frontmatter = page.frontmatter or {}
    issues: list[Issue] = []
    canonical_source = frontmatter.get("canonical_source")
    raw_sha256 = frontmatter.get("raw_sha256")
    if not canonical_source:
        issues.append(_issue(page, "source-manifest-missing-canonical-source", "Source manifests require canonical_source."))
    if not raw_sha256:
        issues.append(_issue(page, "source-manifest-missing-raw-sha256", "Source manifests require raw_sha256."))

    lines = page.body.splitlines()
    title = frontmatter.get("title")
    h1_lines = [(number, line) for number, line in enumerate(lines, start=1) if line.startswith("# ")]
    if len(h1_lines) != 1 or h1_lines[0][1] != f"# {title}":
        issues.append(
            _issue(
                page,
                "source-manifest-heading",
                "Source manifests require one H1 equal to the frontmatter title.",
            )
        )

    heading_lines = [(number, line) for number, line in enumerate(lines, start=1) if re.match(r"^#{1,6} ", line)]
    disallowed_headings = [line for _number, line in heading_lines if line.startswith("##") and line not in SOURCE_MANIFEST_LEVEL_TWO_HEADINGS]
    if any(line.startswith("###") for _number, line in heading_lines) or disallowed_headings:
        issues.append(
            _issue(
                page,
                "source-manifest-semantic-body",
                "Source manifests may contain only Summary, Registration, and Durable Knowledge Links sections.",
            )
        )
    if any(line.startswith("```") for line in lines):
        issues.append(_issue(page, "source-manifest-semantic-body", "Source manifests may not contain fenced code."))
    if _contains_claim_table(lines):
        issues.append(_issue(page, "source-manifest-semantic-body", "Source manifests may not contain copied claim tables."))

    section_starts = {line: number for number, line in heading_lines if line in SOURCE_MANIFEST_LEVEL_TWO_HEADINGS}
    for heading in sorted(SOURCE_MANIFEST_LEVEL_TWO_HEADINGS):
        if heading not in section_starts:
            issues.append(_issue(page, "source-manifest-missing-section", f"Source manifest is missing {heading}."))

    issues.extend(_validate_summary(page, lines, frontmatter, canonical_source, section_starts))
    issues.extend(_validate_registration(page, lines, frontmatter, section_starts))
    issues.extend(_validate_durable_links(page, lines, section_starts))
    return issues


def _validate_summary(
    page: Page,
    lines: list[str],
    frontmatter: dict,
    canonical_source: object,
    section_starts: dict[str, int],
) -> list[Issue]:
    summary = frontmatter.get("summary")
    issues: list[Issue] = []
    if not isinstance(summary, str) or "\n" in summary or len(summary) > 240:
        issues.append(
            _issue(page, "source-manifest-summary", "Source manifest summary must be one line of at most 240 characters.")
        )
        return issues

    summary_start = section_starts.get("## Summary")
    registration_start = section_starts.get("## Registration")
    if summary_start is None or registration_start is None:
        return issues
    summary_lines = _section_content(lines, summary_start, registration_start)
    nonblank = [line for line in summary_lines if line]
    expected_source = f"Source: `{canonical_source}`"
    if len(nonblank) < 2 or nonblank[0] != summary or nonblank[1] != expected_source or len(nonblank) != 2:
        issues.append(
            _issue(
                page,
                "source-manifest-summary",
                "Summary must match frontmatter and be followed directly by its Source line.",
            )
        )
    return issues


def _validate_registration(
    page: Page,
    lines: list[str],
    frontmatter: dict,
    section_starts: dict[str, int],
) -> list[Issue]:
    registration_start = section_starts.get("## Registration")
    links_start = section_starts.get("## Durable Knowledge Links")
    if registration_start is None or links_start is None:
        return []
    expected = {
        f"- Canonical source: `{frontmatter.get('canonical_source')}`",
        f"- SHA-256: `{frontmatter.get('raw_sha256')}`",
        f"- Source kind: `{frontmatter.get('source_kind')}`",
    }
    content = {line for line in _section_content(lines, registration_start, links_start) if line}
    if not expected <= content:
        return [_issue(page, "source-manifest-registration", "Source manifest registration is incomplete.")]
    return []


def _validate_durable_links(page: Page, lines: list[str], section_starts: dict[str, int]) -> list[Issue]:
    links_start = section_starts.get("## Durable Knowledge Links")
    if links_start is None:
        return []
    content = [line for line in _section_content(lines, links_start, len(lines) + 1) if line]
    if all(line == "- None." or re.fullmatch(r"- \[\[[^\]]+\]\]", line) for line in content):
        return []
    return [_issue(page, "source-manifest-durable-links", "Durable Knowledge Links must contain only wiki-link bullets or - None." )]


def _validate_duplicate_canonical_bodies(root: Path, pages: list[Page]) -> list[Issue]:
    by_body: dict[str, Page] = {}
    issues: list[Issue] = []
    excluded_roles = {
        PageRole.SOURCE_MANIFEST,
        PageRole.GENERATED_VIEW,
        PageRole.OPERATION_LOG,
        PageRole.AUDIT_REPORT,
    }
    for page in pages:
        if classify_page_role(root, page) in excluded_roles:
            continue
        normalized = _normalize_body(page.body)
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        previous = by_body.get(digest)
        if previous is None:
            by_body[digest] = page
            continue
        previous_path = _relative_path(root, previous.path)
        current_path = _relative_path(root, page.path)
        issues.append(
            _issue(
                page,
                "duplicate-canonical-body",
                f"Review required: canonical body duplicates {previous_path}; do not merge {previous_path} and {current_path} automatically.",
            )
        )
    return issues


def _section_content(lines: list[str], start_line: int, end_line: int) -> list[str]:
    return lines[start_line:end_line - 1]


def _contains_claim_table(lines: list[str]) -> bool:
    return any(re.fullmatch(r"\|\s*Claim\s*\|\s*Status\s*\|\s*Evidence\s*\|", line) for line in lines)


def _normalize_body(body: str) -> str:
    normalized = "\n".join(line.rstrip() for line in body.splitlines())
    return re.sub(r"\n{3,}", "\n\n", normalized)


def _issue(page: Page, code: str, message: str) -> Issue:
    return Issue(severity="error", code=code, path=str(page.path), message=message)


def _relative_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()
