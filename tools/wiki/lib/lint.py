from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .frontmatter import Page, parse_page
from .hash_source import compute_sha256
from .links import find_wiki_links, resolve_wiki_link, wiki_link_stays_in_root
from .review import validate_review_queue
from .schema import Issue, validate_schema


ALLOWED_LOG_EVENTS = {
    "ingest",
    "query",
    "lint",
    "repair",
    "decision",
    "schema-change",
    "manual-note",
}

CORE_INDEX_LINKS = {
    "overview",
    "inbox",
    "log",
    "concepts/compiled-knowledge",
    "concepts/llm-maintained-wiki",
    "concepts/source-of-truth",
    "workflows/ingest-new-source",
    "workflows/wiki-linting",
    "decisions/use-markdown-for-compiled-knowledge",
    "maps/wiki-operating-map",
}

EXCLUDED_NAVIGATION_PAGES = {
    "index",
    "maps/README",
}

ALLOWED_CLAIM_STATUSES = {"stated", "inferred", "contested", "deprecated"}
LIFECYCLE_LIST_FIELDS = {"aliases", "redirects", "supersedes", "superseded_by"}


@dataclass(frozen=True)
class LintResult:
    issues: list[Issue]

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def counts_by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.severity] = counts.get(issue.severity, 0) + 1
        return counts


@dataclass(frozen=True)
class HumanLockRange:
    start_line: int
    end_line: int


@dataclass(frozen=True)
class HumanLockState:
    page_locked: bool
    ranges: list[HumanLockRange]


@dataclass(frozen=True)
class ClaimEvidenceRow:
    claim: str
    status: str
    evidence: str
    line: int


@dataclass(frozen=True)
class RawEvidenceRef:
    path: str
    sha256: str | None


def lint_repo(root: Path | str) -> LintResult:
    repo_root = Path(root)
    wiki_root = repo_root / "wiki"
    issues: list[Issue] = []
    pages: list[Page] = []
    for path in sorted(wiki_root.rglob("*.md")):
        try:
            pages.append(parse_page(path))
        except OSError as exc:
            issues.append(
                Issue(
                    severity="error",
                    code="unreadable-file",
                    path=str(path),
                    message=str(exc),
                )
            )

    for page in pages:
        issues.extend(validate_schema(page))
        issues.extend(validate_page_links(page, wiki_root))
        issues.extend(validate_provenance(page, repo_root))
        issues.extend(validate_lifecycle(page, wiki_root))
        issues.extend(validate_human_locks(page))
        issues.extend(validate_source_hash(page, repo_root))
        if page.path.name == "log.md":
            issues.extend(validate_log(page))

    issues.extend(validate_index_coverage(wiki_root, pages))
    issues.extend(validate_review_queue(repo_root))
    return LintResult(issues=issues)


def render_lint_report(result: LintResult, report_date: date | None = None) -> str:
    current_date = report_date or date.today()
    lines = [
        f"# Wiki Lint Report - {current_date.isoformat()}",
        "",
        "## Summary",
        "",
        f"- Issues found: {len(result.issues)}",
        f"- Errors: {result.counts_by_severity().get('error', 0)}",
        f"- Warnings: {result.counts_by_severity().get('warning', 0)}",
        "",
        "## Issues",
        "",
    ]

    if not result.issues:
        lines.append("- None.")
        lines.extend(["", "## Issues By Severity", "", "- None."])
    else:
        lines.extend(_render_grouped_issues(result.issues))

    lines.extend(
        [
            "",
            "## Applied Fixes",
            "",
            "- None.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_grouped_issues(issues: list[Issue]) -> list[str]:
    lines = ["## Issues By Severity", ""]
    severities = sorted({issue.severity for issue in issues})
    for severity in severities:
        lines.extend([f"### {severity.title()}", ""])
        paths = sorted({issue.path for issue in issues if issue.severity == severity})
        for path in paths:
            lines.extend([f"#### `{path}`", ""])
            for issue in issues:
                if issue.severity != severity or issue.path != path:
                    continue
                location = f":{issue.line}" if issue.line is not None else ""
                lines.append(f"- `{issue.code}`{location} - {issue.message}")
            lines.append("")
    return lines


def validate_page_links(page: Page, wiki_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for target in find_wiki_links(page.body):
        resolved = resolve_wiki_link(target, wiki_root)
        if not wiki_link_stays_in_root(target, wiki_root):
            issues.append(
                Issue(
                    severity="error",
                    code="wiki-link-outside-root",
                    path=str(page.path),
                    message=f"Wiki link must stay under wiki/: [[{target}]].",
                )
            )
            continue
        if not resolved.exists():
            issues.append(
                Issue(
                    severity="error",
                    code="broken-link",
                    path=str(page.path),
                    message=f"Broken wiki link: [[{target}]].",
                )
            )
    return issues


def validate_index_coverage(wiki_root: Path, pages: list[Page] | None = None) -> list[Issue]:
    index_path = wiki_root / "index.md"
    if not index_path.exists():
        return [
            Issue(
                severity="error",
                code="missing-index",
                path=str(index_path),
                message="wiki/index.md is missing.",
            )
        ]

    index_text = index_path.read_text()
    map_text = "\n".join(path.read_text() for path in sorted((wiki_root / "maps").glob("*.md")))
    present_links = set(find_wiki_links(index_text))
    navigation_links = present_links | set(find_wiki_links(map_text))
    issues: list[Issue] = []
    for target in sorted(CORE_INDEX_LINKS - present_links):
        issues.append(
            Issue(
                severity="error",
                code="missing-index-link",
                path=str(index_path),
                message=f"Core page missing from index: [[{target}]].",
            )
            )
    for target in present_links:
        if not wiki_link_stays_in_root(target, wiki_root):
            issues.append(
                Issue(
                    severity="error",
                    code="wiki-link-outside-root",
                    path=str(index_path),
                    message=f"Index wiki link must stay under wiki/: [[{target}]].",
                )
            )
            continue
        if not resolve_wiki_link(target, wiki_root).exists():
            issues.append(
                Issue(
                    severity="error",
                    code="broken-index-link",
                    path=str(index_path),
                    message=f"Index links to missing page: [[{target}]].",
                )
            )
    for target in sorted(_discover_page_targets(wiki_root, pages) - navigation_links):
        issues.append(
            Issue(
                severity="error",
                code="orphan-page",
                path=str(resolve_wiki_link(target, wiki_root)),
                message=f"Durable page is not linked from index or maps: [[{target}]].",
            )
        )
    return issues


def validate_log(page: Page) -> list[Issue]:
    issues: list[Issue] = []
    event_pattern = re.compile(r"^## \[(\d{4}-\d{2}-\d{2})\] ([a-z-]+) \| .+$")
    event_entries: list[tuple[str, str]] = []
    for line_no, line in enumerate(page.body.splitlines(), start=1):
        if not line.startswith("## "):
            continue
        if line == "## Event Types":
            continue
        match = event_pattern.match(line)
        if match is None:
            issues.append(
                Issue(
                    severity="error",
                    code="invalid-log-heading",
                    path=str(page.path),
                    message="Log heading must use: ## [YYYY-MM-DD] event-type | Short Title.",
                    line=line_no,
                )
            )
            continue
        event_entries.append((match.group(1), match.group(2)))
    if not event_entries:
        issues.append(
            Issue(
                severity="error",
                code="missing-log-entry",
                path=str(page.path),
                message="Log must contain at least one parseable event entry.",
            )
        )
        return issues

    for _date, event_type in event_entries:
        if event_type not in ALLOWED_LOG_EVENTS:
            issues.append(
                Issue(
                    severity="error",
                    code="invalid-log-event",
                    path=str(page.path),
                    message=f"Invalid log event type: {event_type}.",
                )
            )
    return issues


def validate_provenance(page: Page, repo_root: Path | None = None) -> list[Issue]:
    if not page.frontmatter:
        return []

    quality = page.frontmatter.get("quality")
    if not isinstance(quality, dict):
        return []

    provenance = quality.get("provenance")
    source_count = page.frontmatter.get("source_count")
    issues: list[Issue] = []

    if provenance == "none" and str(source_count) not in {"0", "0.0"}:
        issues.append(
            Issue(
                severity="error",
                code="source-count-without-provenance",
                path=str(page.path),
                message="source_count must be 0 when quality.provenance is none.",
            )
        )

    if provenance in {"section", "claim"} and not _has_provenance_signal(page.body):
        issues.append(
            Issue(
                severity="error",
                code="missing-provenance-signal",
                path=str(page.path),
                message="Page claims section/claim provenance but no provenance signal was found.",
            )
        )

    if provenance == "claim":
        issues.extend(_validate_claim_level_provenance(page, repo_root, source_count))

    return issues


def validate_human_locks(page: Page) -> list[Issue]:
    issues: list[Issue] = []
    stack: list[int] = []
    for line_no, line in enumerate(page.body.splitlines(), start=1):
        if "<!-- human-locked:start -->" in line:
            stack.append(line_no)
        if "<!-- human-locked:end -->" in line:
            if not stack:
                issues.append(
                    Issue(
                        severity="error",
                        code="misordered-human-lock",
                        path=str(page.path),
                        message="Human lock end marker appears before a start marker.",
                        line=line_no,
                    )
                )
            else:
                stack.pop()

    for line_no in stack:
        issues.append(
            Issue(
                severity="error",
                code="unmatched-human-lock",
                path=str(page.path),
                message="Human lock section marker is unmatched.",
                line=line_no,
            )
        )
    return issues


def validate_lifecycle(page: Page, wiki_root: Path) -> list[Issue]:
    if not page.frontmatter:
        return []

    issues: list[Issue] = []
    for field in sorted(LIFECYCLE_LIST_FIELDS):
        value = page.frontmatter.get(field)
        if value is not None and not isinstance(value, list):
            issues.append(
                Issue(
                    severity="error",
                    code="invalid-lifecycle-field",
                    path=str(page.path),
                    message=f"{field} must be a list when present.",
                )
            )
            continue
        if field in {"supersedes", "superseded_by", "redirects"} and isinstance(value, list):
            for target in value:
                _validate_lifecycle_target(issues, page, wiki_root, field, str(target))

    if page.frontmatter.get("status") == "deprecated" and not page.frontmatter.get("superseded_by"):
        issues.append(
            Issue(
                severity="error",
                code="deprecated-page-without-superseded-by",
                path=str(page.path),
                message="Deprecated pages must declare superseded_by.",
            )
        )
    return issues


def _validate_lifecycle_target(
    issues: list[Issue],
    page: Page,
    wiki_root: Path,
    field: str,
    raw_target: str,
) -> None:
    target = _normalize_lifecycle_target(raw_target)
    if not target:
        issues.append(
            Issue(
                severity="error",
                code="missing-supersession-target",
                path=str(page.path),
                message=f"{field} contains an empty target.",
            )
        )
        return
    if not resolve_wiki_link(target, wiki_root).exists():
        issues.append(
            Issue(
                severity="error",
                code="missing-supersession-target",
                path=str(page.path),
                message=f"{field} target does not exist: {raw_target}.",
            )
        )


def _normalize_lifecycle_target(target: str) -> str:
    normalized = target.strip()
    if normalized.startswith("wiki/"):
        normalized = normalized[len("wiki/") :]
    if normalized.endswith(".md"):
        normalized = normalized[:-3]
    return normalized


def validate_source_hash(page: Page, repo_root: Path) -> list[Issue]:
    if not page.frontmatter or page.frontmatter.get("type") != "source":
        return []

    canonical_source = page.frontmatter.get("canonical_source")
    if not canonical_source:
        return []

    source_path = repo_root / str(canonical_source)
    issues: list[Issue] = []
    raw_root = (repo_root / "raw").resolve()
    try:
        source_path.resolve().relative_to(raw_root)
    except ValueError:
        issues.append(
            Issue(
                severity="error",
                code="source-outside-raw",
                path=str(page.path),
                message=f"canonical_source must point under raw/: {canonical_source}.",
            )
        )
        return issues

    if not source_path.exists():
        return [
            Issue(
                severity="error",
                code="missing-canonical-source",
                path=str(page.path),
                message=f"canonical_source does not exist: {canonical_source}.",
            )
        ]

    expected_hash = page.frontmatter.get("raw_sha256")
    if not expected_hash:
        return [
            Issue(
                severity="warning",
                code="missing-source-hash",
                path=str(page.path),
                message=f"Source page has canonical_source but no raw_sha256: {canonical_source}.",
            )
        ]

    actual_hash = compute_sha256(source_path)
    if actual_hash != expected_hash:
        return [
            Issue(
                severity="error",
                code="source-drift",
                path=str(page.path),
                message=f"raw_sha256 does not match {canonical_source}.",
            )
        ]
    return []


def find_human_locks(page: Page) -> HumanLockState:
    page_locked = "<!-- human-locked -->" in page.body
    ranges: list[HumanLockRange] = []
    stack: list[int] = []
    for line_no, line in enumerate(page.body.splitlines(), start=1):
        if "<!-- human-locked:start -->" in line:
            stack.append(line_no)
        if "<!-- human-locked:end -->" in line and stack:
            ranges.append(HumanLockRange(start_line=stack.pop(), end_line=line_no))
    return HumanLockState(page_locked=page_locked, ranges=ranges)


def _has_provenance_signal(body: str) -> bool:
    signals = [
        "Source:",
        "Evidence:",
        "<!-- provenance:",
        "## Supporting Sources",
        "| Claim |",
    ]
    return any(signal in body for signal in signals)


def _validate_claim_level_provenance(page: Page, repo_root: Path | None, source_count: object) -> list[Issue]:
    rows = _extract_claim_evidence_rows(page.body)
    if not rows:
        return [
            Issue(
                severity="error",
                code="missing-claim-evidence-table",
                path=str(page.path),
                message="quality.provenance is claim, but no Claim/Status/Evidence table was found.",
            )
        ]

    issues: list[Issue] = []
    evidence_refs: set[str] = set()
    for row in rows:
        status = row.status.strip().lower()
        if status not in ALLOWED_CLAIM_STATUSES:
            issues.append(
                Issue(
                    severity="error",
                    code="invalid-claim-status",
                    path=str(page.path),
                    message=f"Claim status must be one of {sorted(ALLOWED_CLAIM_STATUSES)}: {row.status}.",
                    line=row.line,
                )
            )

        evidence = row.evidence.strip()
        if not evidence:
            issues.append(
                Issue(
                    severity="error",
                    code="missing-claim-evidence",
                    path=str(page.path),
                    message="Claim evidence must be non-empty.",
                    line=row.line,
                )
            )
            continue

        row_refs = _extract_claim_evidence_refs(evidence)
        if not row_refs:
            issues.append(
                Issue(
                    severity="error",
                    code="invalid-claim-evidence",
                    path=str(page.path),
                    message="Claim evidence must link to raw, source, wiki, or explicit human-instruction evidence.",
                    line=row.line,
                )
            )
            continue
        evidence_refs.update(row_refs)
        issues.extend(_validate_claim_evidence_sources(page, evidence, repo_root, row.line))

    expected_source_count = _coerce_source_count(source_count)
    if expected_source_count is not None and expected_source_count != len(evidence_refs):
        issues.append(
            Issue(
                severity="error",
                code="source-count-evidence-mismatch",
                path=str(page.path),
                message=(
                    "source_count must match unique claim evidence refs: "
                    f"source_count={expected_source_count}, evidence_refs={len(evidence_refs)}."
                ),
            )
        )
    return issues


def _extract_claim_evidence_rows(body: str) -> list[ClaimEvidenceRow]:
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        headers = [_normalize_table_header(cell) for cell in _split_markdown_table_row(line)]
        required = {"claim", "status", "evidence"}
        if not required.issubset(set(headers)):
            continue
        header_index = {header: headers.index(header) for header in required}
        rows: list[ClaimEvidenceRow] = []
        for row_index in range(index + 1, len(lines)):
            row_line = lines[row_index]
            if not row_line.lstrip().startswith("|"):
                break
            cells = _split_markdown_table_row(row_line)
            if _is_markdown_table_separator(cells):
                continue
            if len(cells) <= max(header_index.values()):
                continue
            rows.append(
                ClaimEvidenceRow(
                    claim=cells[header_index["claim"]].strip(),
                    status=cells[header_index["status"]].strip(),
                    evidence=cells[header_index["evidence"]].strip(),
                    line=row_index + 1,
                )
            )
        return rows
    return []


def _split_markdown_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _normalize_table_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.strip().lower())


def _is_markdown_table_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _extract_claim_evidence_refs(evidence: str) -> set[str]:
    refs = {f"wiki:{target}" for target in find_wiki_links(evidence)}
    refs.update(f"raw:{ref.path}" for ref in _extract_raw_evidence_refs(evidence))
    if "Human instruction" in evidence or "explicit human instruction" in evidence.lower():
        refs.add("human:instruction")
    return refs


def _extract_raw_evidence_refs(evidence: str) -> list[RawEvidenceRef]:
    refs: list[RawEvidenceRef] = []
    for match in re.finditer(r"raw/[^\s`|,;)>\]]+", evidence):
        token = match.group(0).rstrip(".")
        sha256 = None
        if "#sha256=" in token:
            token, sha256 = token.split("#sha256=", 1)
        refs.append(RawEvidenceRef(path=token, sha256=sha256))
    return refs


def _validate_claim_evidence_sources(
    page: Page,
    evidence: str,
    repo_root: Path | None,
    line: int,
) -> list[Issue]:
    if repo_root is None:
        return []

    issues: list[Issue] = []
    for raw_ref in _extract_raw_evidence_refs(evidence):
        issues.extend(_validate_raw_claim_evidence(page, raw_ref, repo_root, line))
    for target in find_wiki_links(evidence):
        if target.startswith("sources/"):
            source_page = resolve_wiki_link(target, repo_root / "wiki")
            if source_page.exists():
                issues.extend(_claim_source_hash_issues(parse_page(source_page), repo_root, str(page.path), line))
    return issues


def _validate_raw_claim_evidence(
    page: Page,
    raw_ref: RawEvidenceRef,
    repo_root: Path,
    line: int,
) -> list[Issue]:
    raw_path = repo_root / raw_ref.path
    raw_root = (repo_root / "raw").resolve()
    try:
        raw_path.resolve().relative_to(raw_root)
    except ValueError:
        return [
            Issue(
                severity="error",
                code="invalid-claim-evidence",
                path=str(page.path),
                message=f"Claim raw evidence must point under raw/: {raw_ref.path}.",
                line=line,
            )
        ]

    if not raw_path.exists():
        return [
            Issue(
                severity="error",
                code="claim-evidence-raw-missing",
                path=str(page.path),
                message=f"Claim raw evidence does not exist: {raw_ref.path}.",
                line=line,
            )
        ]

    actual_hash = compute_sha256(raw_path)
    if raw_ref.sha256 is not None and raw_ref.sha256 != actual_hash:
        return [
            Issue(
                severity="error",
                code="claim-evidence-source-hash-invalid",
                path=str(page.path),
                message=f"Claim raw evidence hash does not match: {raw_ref.path}.",
                line=line,
            )
        ]

    source_page = _find_source_page_for_raw_ref(page, repo_root, raw_ref.path)
    if source_page is None:
        if raw_ref.sha256 == actual_hash:
            return []
        return [
            Issue(
                severity="error",
                code="claim-evidence-source-unregistered",
                path=str(page.path),
                message=f"Claim raw evidence has no registered source page or inline SHA-256: {raw_ref.path}.",
                line=line,
            )
        ]
    return _claim_source_hash_issues(source_page, repo_root, str(page.path), line)


def _find_source_page_for_raw_ref(page: Page, repo_root: Path, raw_path: str) -> Page | None:
    if page.frontmatter and page.frontmatter.get("type") == "source":
        if page.frontmatter.get("canonical_source") == raw_path:
            return page

    for source_path in sorted((repo_root / "wiki" / "sources").glob("*.md")):
        source_page = parse_page(source_path)
        if not source_page.frontmatter or source_page.frontmatter.get("type") != "source":
            continue
        if source_page.frontmatter.get("canonical_source") == raw_path:
            return source_page
    return None


def _claim_source_hash_issues(source_page: Page, repo_root: Path, claim_page_path: str, line: int) -> list[Issue]:
    issues: list[Issue] = []
    for source_issue in validate_source_hash(source_page, repo_root):
        issues.append(
            Issue(
                severity="error",
                code="claim-evidence-source-hash-invalid",
                path=claim_page_path,
                message=f"Claim evidence source page is not hash-valid: {source_issue.message}",
                line=line,
            )
        )
    return issues


def _coerce_source_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _discover_page_targets(wiki_root: Path, pages: list[Page] | None) -> set[str]:
    if pages is None:
        paths = sorted(wiki_root.rglob("*.md"))
    else:
        paths = [page.path for page in pages]

    targets: set[str] = set()
    for path in paths:
        relative = path.relative_to(wiki_root).with_suffix("")
        target = relative.as_posix()
        if target in EXCLUDED_NAVIGATION_PAGES:
            continue
        targets.add(target)
    return targets
