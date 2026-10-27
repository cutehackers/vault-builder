"""Turn a patch-page draft into the full page it describes.

A patch draft names an existing page and a list of small edits. The edits are
applied to the current page text, and the result goes through the same
validation and write path as an upsert-page draft. This keeps one validation
path while letting agents record one decision or correction without
rewriting the whole page.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from .frontmatter import parse_page
from .lint import (
    ClaimEvidenceRow,
    ClaimEvidenceTable,
    claim_evidence_table,
    count_claim_evidence_refs,
    fenced_line_flags,
    split_markdown_table_row,
)

PATCH_OPERATIONS = {
    "append-claim",
    "supersede-claim",
    "set-claim-status",
    "replace-section",
    "append-to-section",
}
_CLAIM_OPERATIONS = {"append-claim", "supersede-claim", "set-claim-status"}
SUPERSEDED_MARKER = "Superseded by:"  # lint matches it case-insensitively as SUPERSEDED_BY_MARKER
_HEADING = re.compile(r"^(#{1,6})\s+\S")


def materialize_patch(target_path: Path, patch: dict[str, Any]) -> dict[str, Any]:
    """Return an upsert-page draft whose frontmatter and body apply the patch edits."""
    if not target_path.exists():
        raise ValueError(f"patch-page target does not exist: {target_path}")
    edits = patch.get("edits")
    if not isinstance(edits, list) or not edits:
        raise ValueError("Patch draft edits must be a non-empty list.")
    frontmatter_updates = patch.get("frontmatter", {})
    if not isinstance(frontmatter_updates, dict):
        raise ValueError("Patch draft frontmatter must be an object when present.")

    existing = parse_page(target_path)
    frontmatter = dict(existing.frontmatter or {})
    quality = frontmatter.get("quality")
    claim_page = isinstance(quality, dict) and quality.get("provenance") == "claim"
    claim_ops = sorted({edit.get("op") for edit in edits if isinstance(edit, dict)} & _CLAIM_OPERATIONS)
    if claim_ops and not claim_page:
        raise ValueError(
            f"Claim edits {claim_ops} need quality.provenance: claim on {target_path.name}, "
            "because lint checks claim evidence only on those pages. Use an upsert-page draft to change provenance."
        )
    lines = existing.body.splitlines()
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError(f"Patch draft edits[{index}] must be an object.")
        op = edit.get("op")
        if op not in PATCH_OPERATIONS:
            raise ValueError(f"Patch draft edits[{index}].op must be one of {sorted(PATCH_OPERATIONS)}.")
        try:
            lines = _apply_edit(lines, edit)
        except ValueError as exc:
            raise ValueError(f"Patch draft edits[{index}] ({op}): {exc}") from None
    # parse_page keeps the blank line after the frontmatter and render_page adds it again.
    body = "\n".join(lines).lstrip("\n") + "\n"

    frontmatter["updated"] = date.today().isoformat()
    if claim_page:
        frontmatter["source_count"] = count_claim_evidence_refs(body)
    frontmatter.update(frontmatter_updates)

    upsert = {key: value for key, value in patch.items() if key not in {"operation", "edits", "frontmatter"}}
    upsert.update({"operation": "upsert-page", "frontmatter": frontmatter, "body": body})
    return upsert


def _apply_edit(lines: list[str], edit: dict[str, Any]) -> list[str]:
    op = edit["op"]
    if op == "append-claim":
        return _append_claim(lines, _claim_cells(edit, "claim", "status", "evidence"))
    if op == "supersede-claim":
        return _supersede_claim(lines, edit)
    if op == "set-claim-status":
        return _set_claim_status(lines, _text(edit, "claim"), _cell(edit, "status"))
    if op == "replace-section":
        return _edit_section(lines, _text(edit, "heading"), _content(edit), replace=True)
    return _edit_section(lines, _text(edit, "heading"), _content(edit), replace=False)


def _append_claim(lines: list[str], cells: dict[str, str]) -> list[str]:
    table = _require_table(lines)
    return [*lines[: table.end_line], _render_row(table.headers, cells), *lines[table.end_line :]]


def _supersede_claim(lines: list[str], edit: dict[str, Any]) -> list[str]:
    old_claim = _text(edit, "claim")
    replacement = _claim_cells(edit, "by", "status", "evidence")
    table = _require_table(lines)
    row = _find_row(table.rows, old_claim)
    if row.status.strip().lower() == "superseded":
        raise ValueError(f"claim is already superseded: {old_claim}")
    updated = {
        "claim": row.claim,
        "status": "superseded",
        "evidence": f"{row.evidence.rstrip(' .;')}; {SUPERSEDED_MARKER} {replacement['claim']}",
    }
    result = list(lines)
    result[row.line - 1] = _render_row(table.headers, updated, original=lines[row.line - 1])
    result.insert(row.line, _render_row(table.headers, replacement))
    return result


def _set_claim_status(lines: list[str], claim: str, status: str) -> list[str]:
    table = _require_table(lines)
    row = _find_row(table.rows, claim)
    updated = {"claim": row.claim, "status": status, "evidence": row.evidence}
    result = list(lines)
    result[row.line - 1] = _render_row(table.headers, updated, original=lines[row.line - 1])
    return result


def _edit_section(lines: list[str], heading: str, content: str, *, replace: bool) -> list[str]:
    match = _HEADING.match(heading)
    if not match:
        raise ValueError(f"heading must be a markdown heading line such as '## Notes': {heading}")
    level = len(match.group(1))
    fenced = fenced_line_flags(lines)
    starts = [
        index for index, line in enumerate(lines) if not fenced[index] and line.strip() == heading.strip()
    ]
    if len(starts) != 1:
        raise ValueError(f"heading must appear exactly once, found {len(starts)}: {heading}")
    start = starts[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        other = _HEADING.match(lines[index])
        if other and not fenced[index] and len(other.group(1)) <= level:
            end = index
            break
    content_lines = content.strip("\n").split("\n")
    if replace:
        return [*lines[: start + 1], "", *content_lines, "", *lines[end:]]
    section_end = end
    while section_end > start + 1 and not lines[section_end - 1].strip():
        section_end -= 1
    return [*lines[:section_end], "", *content_lines, "", *lines[end:]]


def _require_table(lines: list[str]) -> ClaimEvidenceTable:
    table = claim_evidence_table("\n".join(lines))
    if table is None:
        raise ValueError("page has no Claim/Status/Evidence table; use an upsert-page draft to add one")
    return table


def _find_row(rows: list[ClaimEvidenceRow], claim: str) -> ClaimEvidenceRow:
    matches = [row for row in rows if row.claim.strip() == claim.strip()]
    if len(matches) != 1:
        raise ValueError(f"claim must match exactly one row, found {len(matches)}: {claim}")
    return matches[0]


def _render_row(headers: list[str], cells: dict[str, str], original: str | None = None) -> str:
    existing = _split_row(original) if original is not None else []
    rendered = []
    for index, header in enumerate(headers):
        if header in cells:
            rendered.append(cells[header])
        elif index < len(existing):
            rendered.append(existing[index])
        else:
            rendered.append("")
    return "| " + " | ".join(rendered) + " |"


def _split_row(line: str) -> list[str]:
    return split_markdown_table_row(line)


def _claim_cells(edit: dict[str, Any], claim_key: str, status_key: str, evidence_key: str) -> dict[str, str]:
    return {
        "claim": _cell(edit, claim_key),
        "status": _cell(edit, status_key),
        "evidence": _cell(edit, evidence_key),
    }


def _cell(edit: dict[str, Any], key: str) -> str:
    value = _text(edit, key)
    if "|" in value or "\n" in value:
        raise ValueError(f"{key} must not contain '|' or a line break")
    return value


def _text(edit: dict[str, Any], key: str) -> str:
    value = edit.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _content(edit: dict[str, Any]) -> str:
    value = edit.get("content")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("content must be a non-empty string")
    return value
