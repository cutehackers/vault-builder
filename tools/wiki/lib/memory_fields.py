"""Lint rules for the optional memory frontmatter fields and the status `retired`.

Fields: memory_schema, pin, memory_kind, retired_reason, retired_at. A page without
any of them is checked by nothing here, so existing pages stay valid.

Rules:
1. pin or memory_kind needs memory_schema.
2. memory_schema must be a known value.
3. pin must be a boolean and memory_kind must be an allowed kind.
4. status retired needs retired_reason and retired_at.
5. retired_reason and retired_at appear only when status is retired.

This module imports only schema and frontmatter.
"""
from __future__ import annotations

from datetime import date

from .frontmatter import Page
from .schema import Issue

ALLOWED_MEMORY_SCHEMAS = frozenset({"vault-memory/v1"})
ALLOWED_MEMORY_KINDS = frozenset({"rule", "correction", "decision", "preference"})
RETIRED_STATUS = "retired"
_RETIRED_FIELDS = ("retired_reason", "retired_at")


def validate_memory_fields(page: Page) -> list[Issue]:
    frontmatter = page.frontmatter
    if not frontmatter:
        return []
    issues: list[Issue] = []

    def error(code: str, message: str) -> None:
        issues.append(Issue(severity="error", code=code, path=str(page.path), message=message))

    has_schema = "memory_schema" in frontmatter
    if not has_schema:
        for field in ("pin", "memory_kind"):
            if field in frontmatter:
                error("memory-field-without-schema", f"{field} needs memory_schema.")
                break
    elif frontmatter["memory_schema"] not in ALLOWED_MEMORY_SCHEMAS:
        error(
            "invalid-memory-schema",
            f"memory_schema has unknown value {frontmatter['memory_schema']!r}. "
            f"Allowed: {', '.join(sorted(ALLOWED_MEMORY_SCHEMAS))}.",
        )

    if "pin" in frontmatter and not isinstance(frontmatter["pin"], bool):
        error("invalid-pin", f"pin must be true or false, not {frontmatter['pin']!r}.")
    if "memory_kind" in frontmatter and frontmatter["memory_kind"] not in ALLOWED_MEMORY_KINDS:
        error(
            "invalid-memory-kind",
            f"memory_kind has invalid value {frontmatter['memory_kind']!r}. "
            f"Allowed: {', '.join(sorted(ALLOWED_MEMORY_KINDS))}.",
        )

    if frontmatter.get("status") == RETIRED_STATUS:
        reason = frontmatter.get("retired_reason")
        if not isinstance(reason, str) or not reason.strip():
            error("retired-without-reason", "Retired pages must declare a non-empty retired_reason.")
        if "retired_at" not in frontmatter or frontmatter["retired_at"] in ("", None):
            error("retired-without-date", "Retired pages must declare retired_at.")
        elif not _is_iso_date(frontmatter["retired_at"]):
            error("invalid-retired-at", f"retired_at must be an ISO date, not {frontmatter['retired_at']!r}.")
    else:
        for field in _RETIRED_FIELDS:
            if field in frontmatter:
                error("retired-field-without-retired-status", f"{field} is allowed only when status is retired.")
    return issues


def _is_iso_date(value: object) -> bool:
    if isinstance(value, date):
        return True
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True
