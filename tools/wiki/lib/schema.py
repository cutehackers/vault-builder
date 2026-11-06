from __future__ import annotations

import re
from dataclasses import dataclass

from .frontmatter import Page


REQUIRED_FIELDS = {
    "title",
    "type",
    "status",
    "created",
    "updated",
    "owner",
    "summary",
    "source_count",
    "tags",
    "related",
    "confidence",
    "quality",
}

REQUIRED_QUALITY_FIELDS = {
    "provenance",
    "links",
    "contradictions",
    "review_required",
}

ALLOWED_TYPES = {
    "source",
    "concept",
    "entity",
    "system",
    "workflow",
    "decision",
    "map",
    "claim",
    "comparison",
    "timeline",
    "question",
    "glossary",
    "index",
    "log",
    "inbox",
    "overview",
    "project",
}
# A project brief (vault-graph request S9) lives at wiki/projects/<project>.md and names that project.
PROJECT_TYPE = "project"
PROJECT_PAGE_DIR = "projects"
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # Same as lint.PROJECT_ID_PATTERN.

ALLOWED_STATUS = {"seed", "active", "stable", "contested", "deprecated", "retired"}
# Pages with these statuses are not current. `deprecated` has a replacement in superseded_by.
# `retired` has none and carries retired_reason and retired_at instead.
NOT_CURRENT_STATUSES = frozenset({"deprecated", "retired"})
ALLOWED_OWNER = {"agent", "human"}
ALLOWED_CONFIDENCE = {"low", "medium", "high"}
ALLOWED_PROVENANCE = {"none", "partial", "section", "claim"}
ALLOWED_LINKS = {"unchecked", "valid", "broken"}
ALLOWED_CONTRADICTIONS = {"unchecked", "none", "present", "contested"}


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    path: str
    message: str
    line: int | None = None


def validate_schema(page: Page) -> list[Issue]:
    parse_issues = [
        Issue(
            severity="error",
            code="invalid-frontmatter",
            path=str(page.path),
            message=error,
        )
        for error in page.parse_errors
    ]

    if page.frontmatter is None:
        return parse_issues + [
            Issue(
                severity="error",
                code="missing-frontmatter",
                path=str(page.path),
                message="Page is missing YAML frontmatter.",
            )
        ]

    issues: list[Issue] = list(parse_issues)
    frontmatter = page.frontmatter

    for field in sorted(REQUIRED_FIELDS - set(frontmatter)):
        issues.append(
            Issue(
                severity="error",
                code="missing-field",
                path=str(page.path),
                message=f"Missing required frontmatter field: {field}.",
            )
        )

    quality = frontmatter.get("quality")
    if not isinstance(quality, dict):
        issues.append(
            Issue(
                severity="error",
                code="invalid-quality",
                path=str(page.path),
                message="quality must be a mapping.",
            )
        )
    else:
        for field in sorted(REQUIRED_QUALITY_FIELDS - set(quality)):
            issues.append(
                Issue(
                    severity="error",
                    code="missing-quality-field",
                    path=str(page.path),
                    message=f"Missing required quality field: {field}.",
                )
            )
        _validate_enum(issues, page, "quality.provenance", quality.get("provenance"), ALLOWED_PROVENANCE)
        _validate_enum(issues, page, "quality.links", quality.get("links"), ALLOWED_LINKS)
        _validate_enum(
            issues,
            page,
            "quality.contradictions",
            quality.get("contradictions"),
            ALLOWED_CONTRADICTIONS,
        )
        _validate_type(issues, page, "quality.review_required", quality.get("review_required"), bool)

    _validate_enum(issues, page, "type", frontmatter.get("type"), ALLOWED_TYPES)
    _validate_enum(issues, page, "status", frontmatter.get("status"), ALLOWED_STATUS)
    _validate_enum(issues, page, "owner", frontmatter.get("owner"), ALLOWED_OWNER)
    _validate_enum(issues, page, "confidence", frontmatter.get("confidence"), ALLOWED_CONFIDENCE)
    _validate_type(issues, page, "source_count", frontmatter.get("source_count"), int)
    _validate_type(issues, page, "tags", frontmatter.get("tags"), list)
    _validate_type(issues, page, "related", frontmatter.get("related"), list)
    issues.extend(_validate_project_page_path(page))

    return issues


def _in_project_page_dir(page: Page) -> bool:
    parent = page.path.parent
    return parent.name == PROJECT_PAGE_DIR and parent.parent.name == "wiki"


def _validate_project_page_path(page: Page) -> list[Issue]:
    """type project <=> wiki/projects/<project>.md, where <project> is the page's one project id."""
    frontmatter = page.frontmatter or {}
    is_project_type = frontmatter.get("type") == PROJECT_TYPE
    in_dir = _in_project_page_dir(page)
    if not is_project_type and not in_dir:
        return []
    project = frontmatter.get("project")
    problem = None
    if not is_project_type:
        problem = f"Pages in wiki/{PROJECT_PAGE_DIR}/ must have type {PROJECT_TYPE}."
    elif not in_dir:
        problem = f"A {PROJECT_TYPE} page must be at wiki/{PROJECT_PAGE_DIR}/<project>.md."
    elif not isinstance(project, str) or not _PROJECT_ID.match(project):
        problem = f"A {PROJECT_TYPE} page needs one project id in project, not {project!r}."
    elif page.path.stem != project:
        problem = f"A {PROJECT_TYPE} page for project {project} must be named {project}.md, not {page.path.name}."
    if problem is None:
        return []
    return [Issue(severity="error", code="project-page-path", path=str(page.path), message=problem)]


def _validate_enum(
    issues: list[Issue],
    page: Page,
    field: str,
    value: object,
    allowed: set[str],
) -> None:
    if value is None:
        return
    if value not in allowed:
        issues.append(
            Issue(
                severity="error",
                code="invalid-enum",
                path=str(page.path),
                message=f"{field} has invalid value {value!r}.",
            )
        )


def _validate_type(
    issues: list[Issue],
    page: Page,
    field: str,
    value: object,
    expected_type: type,
) -> None:
    if value is None:
        return
    if not isinstance(value, expected_type):
        issues.append(
            Issue(
                severity="error",
                code="invalid-field-type",
                path=str(page.path),
                message=f"{field} must be {expected_type.__name__}.",
            )
        )
