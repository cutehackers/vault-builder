from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .frontmatter import parse_page, render_page
from .hash_source import display_path
from .schema import Issue
from .wiki_update import append_log_entry


ALLOWED_REVIEW_TYPES = {"contradiction", "merge", "contested-claim", "human-lock"}
ALLOWED_REVIEW_STATUSES = {"pending", "accepted", "rejected", "resolved"}
FINAL_REVIEW_STATUSES = {"accepted", "rejected", "resolved"}


@dataclass(frozen=True)
class ReviewItem:
    path: Path
    review_id: str
    review_type: str
    status: str
    summary: str
    related_pages: list[str]
    evidence: list[str]


def create_review_item(
    root: Path,
    review_type: str,
    summary: str,
    context: str = "",
    related_pages: list[str] | None = None,
    evidence: list[str] | None = None,
) -> ReviewItem:
    _validate_review_type(review_type)
    if not summary.strip():
        raise ValueError("Review summary must be non-empty.")

    related_pages = related_pages or []
    evidence = evidence or []
    _validate_related_pages(root, related_pages)
    _validate_evidence_refs(root, evidence)

    review_id = _unique_review_id(root, summary)
    review_path = root / "scratch" / "review" / f"{review_id}.md"
    frontmatter = {
        "review_id": review_id,
        "type": review_type,
        "status": "pending",
        "created": date.today().isoformat(),
        "updated": date.today().isoformat(),
        "summary": summary,
        "related_pages": related_pages,
        "evidence": evidence,
        "resolution": "",
    }
    body = "\n".join(
        [
            f"# {summary}",
            "",
            "## Context",
            "",
            context.strip() or "Pending context.",
            "",
            "## Required Judgment",
            "",
            _required_judgment_for(review_type),
            "",
            "## Resolution",
            "",
            "Pending.",
        ]
    )
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(render_page(frontmatter, body))
    return _read_review_item(review_path)


def list_review_items(root: Path) -> list[ReviewItem]:
    review_root = root / "scratch" / "review"
    if not review_root.exists():
        return []
    items: list[ReviewItem] = []
    for path in sorted(review_root.glob("*.md")):
        if path.name == "README.md":
            continue
        items.append(_read_review_item(path))
    return items


def render_review_list(root: Path, items: list[ReviewItem]) -> str:
    lines = [f"Review items: {len(items)}"]
    for status, count in sorted(Counter(item.status for item in items).items()):
        lines.append(f"{status}: {count}")
    for item in items:
        lines.append(
            f"- {item.status} | {item.review_type} | {display_path(item.path, root)} | {item.summary}"
        )
    return "\n".join(lines) + "\n"


def validate_review_queue(root: Path) -> list[Issue]:
    review_root = root / "scratch" / "review"
    if not review_root.exists():
        return []

    issues: list[Issue] = []
    for path in sorted(review_root.glob("*.md")):
        if path.name == "README.md":
            continue
        page = parse_page(path)
        if not page.frontmatter:
            issues.append(
                Issue(
                    severity="error",
                    code="missing-review-frontmatter",
                    path=str(path),
                    message="Review item must have frontmatter.",
                )
            )
            continue

        review_type = str(page.frontmatter.get("type", ""))
        if review_type not in ALLOWED_REVIEW_TYPES:
            issues.append(
                Issue(
                    severity="error",
                    code="invalid-review-type",
                    path=str(path),
                    message=f"Invalid review type: {review_type}.",
                )
            )

        status = str(page.frontmatter.get("status", ""))
        if status not in ALLOWED_REVIEW_STATUSES:
            issues.append(
                Issue(
                    severity="error",
                    code="invalid-review-status",
                    path=str(path),
                    message=f"Invalid review status: {status}.",
                )
            )

        for field in ("review_id", "created", "updated", "summary"):
            if not str(page.frontmatter.get(field, "")).strip():
                issues.append(
                    Issue(
                        severity="error",
                        code="missing-review-field",
                        path=str(path),
                        message=f"Review item is missing {field}.",
                    )
                )

        if not isinstance(page.frontmatter.get("related_pages", []), list):
            issues.append(
                Issue(
                    severity="error",
                    code="invalid-review-related-pages",
                    path=str(path),
                    message="Review related_pages must be a list.",
                )
            )
        else:
            for related_page in _as_string_list(page.frontmatter.get("related_pages", [])):
                issues.extend(_validate_review_related_page_ref(root, path, related_page))
        if not isinstance(page.frontmatter.get("evidence", []), list):
            issues.append(
                Issue(
                    severity="error",
                    code="invalid-review-evidence",
                    path=str(path),
                    message="Review evidence must be a list.",
                )
            )
        else:
            for evidence_ref in _as_string_list(page.frontmatter.get("evidence", [])):
                issues.extend(_validate_review_evidence_ref(root, path, evidence_ref))
    return issues


def _validate_review_related_page_ref(root: Path, review_path: Path, related_page: str) -> list[Issue]:
    wiki_root = (root / "wiki").resolve()
    path = root / related_page
    try:
        path.resolve().relative_to(wiki_root)
    except ValueError:
        return [
            Issue(
                severity="error",
                code="invalid-review-related-page",
                path=str(review_path),
                message=f"Review related page must stay under wiki/: {related_page}.",
            )
        ]
    if not path.exists():
        return [
            Issue(
                severity="error",
                code="missing-review-related-page",
                path=str(review_path),
                message=f"Review related page does not exist: {related_page}.",
            )
        ]
    return []


def _validate_review_evidence_ref(root: Path, review_path: Path, evidence_ref: str) -> list[Issue]:
    if not evidence_ref.startswith("raw/"):
        return []
    raw_root = (root / "raw").resolve()
    path = root / evidence_ref
    try:
        path.resolve().relative_to(raw_root)
    except ValueError:
        return [
            Issue(
                severity="error",
                code="invalid-review-evidence",
                path=str(review_path),
                message=f"Review raw evidence must stay under raw/: {evidence_ref}.",
            )
        ]
    if not path.exists():
        return [
            Issue(
                severity="error",
                code="missing-review-evidence",
                path=str(review_path),
                message=f"Review evidence does not exist: {evidence_ref}.",
            )
        ]
    return []


def resolve_review_item(root: Path, review_path: Path, status: str, resolution: str) -> ReviewItem:
    if not review_path.is_absolute():
        review_path = root / review_path
    _validate_final_status(status)
    if not resolution.strip():
        raise ValueError("Review resolution must be non-empty.")

    item = _read_review_item(review_path)
    if item.status != "pending":
        raise ValueError(f"Review item is already {item.status}: {display_path(review_path, root)}")

    page = parse_page(review_path)
    assert page.frontmatter is not None
    frontmatter = dict(page.frontmatter)
    frontmatter["status"] = status
    frontmatter["updated"] = date.today().isoformat()
    frontmatter["resolution"] = resolution
    review_path.write_text(render_page(frontmatter, _replace_resolution(page.body, status, resolution)))

    append_log_entry(
        root / "wiki" / "log.md",
        {
            "event_type": "manual-note",
            "title": f"Review {status}: {item.summary}",
            "items": _resolution_log_items(root, review_path, item, resolution),
        },
    )
    return _read_review_item(review_path)


def _read_review_item(path: Path) -> ReviewItem:
    page = parse_page(path)
    if not page.frontmatter:
        raise ValueError(f"Review item must have frontmatter: {path}")
    review_type = str(page.frontmatter.get("type", ""))
    status = str(page.frontmatter.get("status", ""))
    _validate_review_type(review_type)
    if status not in ALLOWED_REVIEW_STATUSES:
        raise ValueError(f"Invalid review status: {status}")
    related_pages = _as_string_list(page.frontmatter.get("related_pages", []))
    evidence = _as_string_list(page.frontmatter.get("evidence", []))
    return ReviewItem(
        path=path,
        review_id=str(page.frontmatter.get("review_id", path.stem)),
        review_type=review_type,
        status=status,
        summary=str(page.frontmatter.get("summary", path.stem)),
        related_pages=related_pages,
        evidence=evidence,
    )


def _validate_review_type(review_type: str) -> None:
    if review_type not in ALLOWED_REVIEW_TYPES:
        raise ValueError(f"Invalid review type: {review_type}")


def _validate_final_status(status: str) -> None:
    if status not in FINAL_REVIEW_STATUSES:
        raise ValueError(f"Review resolution status must be one of {sorted(FINAL_REVIEW_STATUSES)}.")


def _validate_related_pages(root: Path, related_pages: list[str]) -> None:
    for related_page in related_pages:
        path = root / related_page
        wiki_root = (root / "wiki").resolve()
        try:
            path.resolve().relative_to(wiki_root)
        except ValueError:
            raise ValueError(f"Related page must be under wiki/: {related_page}") from None
        if not path.exists():
            raise ValueError(f"Related page does not exist: {related_page}")


def _validate_evidence_refs(root: Path, evidence: list[str]) -> None:
    for ref in evidence:
        if ref.startswith("raw/"):
            path = root / ref
            raw_root = (root / "raw").resolve()
            try:
                path.resolve().relative_to(raw_root)
            except ValueError:
                raise ValueError(f"Evidence path must be under raw/: {ref}") from None
            if not path.exists():
                raise ValueError(f"Evidence path does not exist: {ref}")


def _as_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _unique_review_id(root: Path, summary: str) -> str:
    prefix = f"{date.today().isoformat()}-{_slugify(summary)}"
    review_root = root / "scratch" / "review"
    candidate = prefix
    index = 2
    while (review_root / f"{candidate}.md").exists():
        candidate = f"{prefix}-{index}"
        index += 1
    return candidate


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "review"


def _required_judgment_for(review_type: str) -> str:
    if review_type == "contradiction":
        return "Decide how the conflicting claims should be represented or separated."
    if review_type == "merge":
        return "Decide whether the candidate pages should be merged, redirected, or kept separate."
    if review_type == "contested-claim":
        return "Decide the current claim status and what evidence should be treated as authoritative."
    if review_type == "human-lock":
        return "Decide whether the proposed change may modify human-locked content."
    return "Human judgment required."


def _replace_resolution(body: str, status: str, resolution: str) -> str:
    marker = "## Resolution"
    replacement = "\n".join([marker, "", f"Status: {status}", "", resolution.strip(), ""])
    if marker not in body:
        return body.rstrip() + "\n\n" + replacement
    before = body.split(marker, 1)[0].rstrip()
    return before + "\n\n" + replacement


def _resolution_log_items(root: Path, review_path: Path, item: ReviewItem, resolution: str) -> list[str]:
    items = [f"Review item: `{display_path(review_path, root)}`."]
    if item.related_pages:
        links = ", ".join(_wiki_link_for_path(path) for path in item.related_pages)
        items.append(f"Related pages: {links}.")
    if item.evidence:
        refs = ", ".join(f"`{ref}`" for ref in item.evidence)
        items.append(f"Evidence refs: {refs}.")
    items.append(f"Resolution: {resolution.strip()}")
    return items


def _wiki_link_for_path(path: str) -> str:
    if path.startswith("wiki/"):
        path = path[len("wiki/") :]
    if path.endswith(".md"):
        path = path[:-3]
    return f"[[{path}]]"
