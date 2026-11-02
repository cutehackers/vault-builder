"""Agent-inferred memories wait in the review queue until a person accepts them (contract 4).

`propose` checks a draft without writing the wiki, then stores a pending `memory-proposal`
review item and a copy of the draft under `scratch/review/`. `accept` publishes that copy
with `publish_draft` and marks the item accepted. Until then the wiki does not change.

Both functions return a result and print nothing. The CLI prints the result as text or as
the `vault-review-result/v1` JSON object.

Allowed imports inside `tools/wiki/lib`: publish, review, draft (spec 4.4).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .draft import apply_draft
from .publish import STATUS_PUBLISHED, publish_draft
from .review import (
    MEMORY_PROPOSAL_TYPE,
    create_review_item,
    read_review_fields,
    read_review_item,
    resolve_review_item,
    validate_review_refs,
)

REVIEW_RESULT_FORMAT = "vault-review-result/v1"

STATUS_PROPOSED = "proposed"
STATUS_RESOLVED = "resolved"
STATUS_REJECTED = "rejected"

REASON_DRAFT_INVALID = "draft-invalid"
REASON_REVIEW_INVALID = "review-invalid"
REASON_TARGET_CHANGED = "target-changed"
REASON_IO_ERROR = "io-error"

DRAFT_SUFFIX = ".draft.json"


@dataclass(frozen=True)
class ProposalResult:
    status: str
    review_id: str | None = None
    review_path: str | None = None
    draft_path: str | None = None
    target_path: str | None = None
    messages: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.status == STATUS_PROPOSED else 1

    def to_json(self) -> dict[str, Any]:
        return {
            "format": REVIEW_RESULT_FORMAT,
            "status": self.status,
            "review_id": self.review_id,
            "review_path": self.review_path,
            "draft_path": self.draft_path,
            "target_path": self.target_path,
            "messages": list(self.messages),
        }


@dataclass(frozen=True)
class AcceptResult:
    """`status` is `resolved` when the item reached `review_status`, else `rejected`."""

    status: str
    review_status: str | None
    reason: str | None = None
    review_id: str | None = None
    review_path: str | None = None
    draft_path: str | None = None
    target_path: str | None = None
    written_paths: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    lines: list[tuple[str, str]] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.status == STATUS_RESOLVED else 1

    def to_json(self) -> dict[str, Any]:
        return {
            "format": REVIEW_RESULT_FORMAT,
            "status": self.status,
            "review_status": self.review_status,
            "reason": self.reason,
            "review_id": self.review_id,
            "review_path": self.review_path,
            "draft_path": self.draft_path,
            "target_path": self.target_path,
            "written_paths": list(self.written_paths),
            "messages": list(self.messages),
        }


def propose(root: Path, draft_path: Path, summary: str, evidence: list[str], related: list[str]) -> ProposalResult:
    """Store `draft_path` as a pending memory proposal. A rejected proposal writes no file."""
    try:
        draft_bytes = draft_path.read_bytes()
        validate_review_refs(root, summary, related, evidence)
        checked = apply_draft(root, draft_path, dry_run=True)
    except (OSError, ValueError) as exc:
        return ProposalResult(status=STATUS_REJECTED, messages=[str(exc)])

    review_id = f"{date.today().isoformat()}-memory-{hashlib.sha256(draft_bytes).hexdigest()[:8]}"
    review_root = root / "scratch" / "review"
    review_file = review_root / f"{review_id}.md"
    draft_copy = review_root / f"{review_id}{DRAFT_SUFFIX}"
    if review_file.exists() or draft_copy.exists():
        return ProposalResult(
            status=STATUS_REJECTED,
            messages=[f"Memory proposal already exists: {_relative(root, review_file)}"],
        )

    target = _relative(root, checked.target_path)
    draft_ref = _relative(root, draft_copy)
    try:
        review_root.mkdir(parents=True, exist_ok=True)
        draft_copy.write_bytes(draft_bytes)
        create_review_item(
            root,
            review_type=MEMORY_PROPOSAL_TYPE,
            summary=summary,
            context=_proposal_context(target, draft_ref, _relative(root, review_file)),
            related_pages=related,
            evidence=evidence,
            review_id=review_id,
            extra_frontmatter={
                "draft": draft_ref,
                "target_path": target,
                "target_hash": _file_sha256(checked.target_path),
            },
        )
    except (OSError, ValueError) as exc:
        draft_copy.unlink(missing_ok=True)
        review_file.unlink(missing_ok=True)
        return ProposalResult(status=STATUS_REJECTED, messages=[str(exc)])

    return ProposalResult(
        status=STATUS_PROPOSED,
        review_id=review_id,
        review_path=_relative(root, review_file),
        draft_path=draft_ref,
        target_path=target,
    )


def accept(root: Path, review_path: Path, resolution: str) -> AcceptResult:
    """Publish the draft attached to a pending memory proposal and mark the item accepted.

    The item stays pending when the target page changed after the proposal or the publish
    is rejected.
    """
    if not review_path.is_absolute():
        review_path = root / review_path
    try:
        item = read_review_item(root, review_path)
        fields = read_review_fields(root, review_path)
    except (OSError, ValueError) as exc:
        return AcceptResult(status=STATUS_REJECTED, review_status=None, reason=REASON_REVIEW_INVALID, messages=[str(exc)])

    draft_ref = str(fields.get("draft", "") or "")
    target_ref = str(fields.get("target_path", "") or "")
    expected_hash = str(fields.get("target_hash", "") or "")

    def refused(reason: str, messages: list[str], lines: list[tuple[str, str]] | None = None) -> AcceptResult:
        return AcceptResult(
            status=STATUS_REJECTED,
            review_status=item.status,
            reason=reason,
            review_id=item.review_id,
            review_path=_relative(root, review_path),
            draft_path=draft_ref or None,
            target_path=target_ref or None,
            messages=messages,
            lines=lines or [],
        )

    if item.review_type != MEMORY_PROPOSAL_TYPE:
        return refused(REASON_REVIEW_INVALID, [f"Review item is not a {MEMORY_PROPOSAL_TYPE}: {item.review_type}"])
    if item.status != "pending":
        return refused(REASON_REVIEW_INVALID, [f"Review item is already {item.status}."])
    if not resolution.strip():
        return refused(REASON_REVIEW_INVALID, ["Review resolution must be non-empty."])
    if not draft_ref or not target_ref:
        return refused(REASON_REVIEW_INVALID, ["Memory proposal is missing draft or target_path."])

    if _file_sha256(root / target_ref) != expected_hash:
        return refused(
            REASON_TARGET_CHANGED,
            [f"Target page changed after the proposal: {target_ref}. Reject this item and propose again."],
        )

    published = publish_draft(root, root / draft_ref)
    lines = [(line.stream, line.text) for line in published.lines]
    if published.status != STATUS_PUBLISHED:
        return refused(published.reason or REASON_IO_ERROR, list(published.messages), lines)

    written = ", ".join(published.written_paths)
    try:
        resolve_review_item(root, review_path, "accepted", f"{resolution.strip()} Published: {written}.")
    except (OSError, ValueError) as exc:
        return refused(
            REASON_IO_ERROR,
            [f"Draft was published ({written}) but the review item was not updated: {exc}"],
            lines,
        )

    return AcceptResult(
        status=STATUS_RESOLVED,
        review_status="accepted",
        review_id=item.review_id,
        review_path=_relative(root, review_path),
        draft_path=draft_ref,
        target_path=target_ref,
        written_paths=list(published.written_paths),
        lines=lines,
    )


def _proposal_context(target: str, draft_ref: str, review_ref: str) -> str:
    return "\n".join(
        [
            f"An agent proposes this memory for `{target}`. The draft is `{draft_ref}`.",
            "",
            "Accept to publish it:",
            "",
            f'`python3 tools/wiki/cli.py review resolve {review_ref} --status accepted --resolution "<reason>"`',
        ]
    )


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
