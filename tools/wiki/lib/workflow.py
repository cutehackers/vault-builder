from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
from typing import Literal

from .draft import resolve_draft_target
from .frontmatter import parse_page
from .hash_source import display_path
from .ingest import ingest_source
from .links import find_wiki_links
from .lint import find_human_locks
from .paths import resolve_draft_artifact_path, resolve_report_path


@dataclass(frozen=True)
class WorkflowResult:
    message: str
    report_path: Path | None = None
    draft_path: Path | None = None


QueryMode = Literal["answer-only", "answer-with-report", "answer-and-capture"]


@dataclass(frozen=True)
class QueryCaptureInput:
    question: str
    mode: QueryMode
    answer_summary: str
    consulted_pages: list[str]
    consulted_sources: list[str]
    confidence: str
    contradictions: list[str]
    capture_recommendation: str
    capture_target: str | None = None
    capture_title: str | None = None


def prepare_ingest_workflow(
    root: Path,
    source_path: Path,
    report_path: Path | None = None,
    title: str | None = None,
    summary: str | None = None,
) -> WorkflowResult:
    report_path = resolve_report_path(root, report_path or _default_workflow_ingest_report_path(source_path))

    result = ingest_source(root, source_path, title=title, summary=summary, report_path=report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _render_ingest_workflow_report(
            result.source_page,
            source_path,
            result.raw_hash,
            result.outcome,
        )
    )
    message = "\n".join(
        [
            "Workflow ingest checkpoint completed.",
            f"Source page: {display_path(result.source_page, root)}",
            f"outcome: {result.outcome}",
            "Semantic extraction pending: agent must read the source and create durable wiki drafts if useful.",
            f"Report written: {display_path(report_path, root)}",
        ]
    )
    return WorkflowResult(message=message, report_path=report_path)


def prepare_update_workflow(root: Path, target_path: Path, report_path: Path | None = None) -> WorkflowResult:
    target_path = _resolve_wiki_path(root, target_path)
    if not target_path.exists():
        raise ValueError(f"Target page does not exist: {display_path(target_path, root)}")
    report_path = resolve_report_path(root, report_path or _default_update_report_path(target_path))

    page = parse_page(target_path)
    locks = find_human_locks(page)
    related = page.frontmatter.get("related", []) if page.frontmatter else []
    body_links = find_wiki_links(page.body)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _render_update_preflight_report(
            target_path=target_path,
            root=root,
            page_locked=locks.page_locked,
            locked_ranges=len(locks.ranges),
            related=[str(item) for item in related] if isinstance(related, list) else [],
            body_links=body_links,
            index_state=_index_state(root, target_path),
            log_state=_log_state(root, target_path),
        )
    )
    message = "\n".join(
        [
            "Update preflight completed.",
            f"Target page: {display_path(target_path, root)}",
            f"Page locked: {locks.page_locked}",
            f"Section-level locked ranges: {len(locks.ranges)}",
            "Semantic update draft required: agent must inspect evidence and create a draft before durable changes.",
            f"Report written: {display_path(report_path, root)}",
        ]
    )
    return WorkflowResult(message=message, report_path=report_path)


def prepare_query_workflow(root: Path, question: str, report_path: Path | None = None) -> WorkflowResult:
    if not question.strip():
        raise ValueError("Question must be non-empty.")
    report_path = resolve_report_path(root, report_path or _default_query_report_path(question))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_render_query_report(question))
    message = "\n".join(
        [
            "Query report scaffold prepared.",
            "Semantic answer synthesis required: agent must read wiki pages and fill the report.",
            f"Report written: {display_path(report_path, root)}",
        ]
    )
    return WorkflowResult(message=message, report_path=report_path)


def prepare_query_capture_workflow(
    root: Path,
    query_input: QueryCaptureInput,
    report_path: Path | None = None,
    draft_path: Path | None = None,
) -> WorkflowResult:
    if not query_input.question.strip():
        raise ValueError("Question must be non-empty.")
    _validate_query_mode(query_input.mode)
    _validate_confidence(query_input.confidence)
    _validate_consulted_pages(root, query_input.consulted_pages)
    _validate_consulted_sources(root, query_input.consulted_sources)

    if query_input.mode == "answer-only":
        return WorkflowResult(
            message="\n".join(
                [
                    "Query answer-only checkpoint completed.",
                    "No report or draft written.",
                    "Semantic answer synthesis remains agent work.",
                ]
            )
        )

    if query_input.mode == "answer-with-report":
        report_path = resolve_report_path(root, report_path or _default_query_report_path(query_input.question))
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_render_query_capture_report(query_input))
        return WorkflowResult(
            message="\n".join(
                [
                    "Query report written.",
                    f"Report written: {display_path(report_path, root)}",
                ]
            ),
            report_path=report_path,
        )

    draft_payload = _query_capture_draft(root, query_input)
    report_path = resolve_report_path(root, report_path or _default_query_report_path(query_input.question))
    draft_path = resolve_draft_artifact_path(root, draft_path or _default_query_draft_path(query_input))
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_render_query_capture_report(query_input))
    draft_path.write_text(json.dumps(draft_payload, indent=2) + "\n")
    return WorkflowResult(
        message="\n".join(
            [
                "Query report written.",
                f"Report written: {display_path(report_path, root)}",
                "Query capture draft written.",
                f"Draft written: {display_path(draft_path, root)}",
                "Publish gate required: run `publish-draft` when the draft is ready for durable wiki state.",
            ]
        ),
        report_path=report_path,
        draft_path=draft_path,
    )


def ensure_draft_targets_workflow_target(root: Path, draft_path: Path, target_path: Path) -> None:
    expected_target = _resolve_wiki_path(root, target_path)
    draft_target = resolve_draft_target(root, draft_path)
    if draft_target.resolve() != expected_target.resolve():
        raise ValueError(
            "Draft target does not match workflow target: "
            f"{display_path(draft_target, root)} != {display_path(expected_target, root)}"
        )


def _resolve_wiki_path(root: Path, path: Path) -> Path:
    if not path.is_absolute():
        path = root / path
    wiki_root = (root / "wiki").resolve()
    try:
        path.resolve().relative_to(wiki_root)
    except ValueError:
        raise ValueError("Workflow target must stay under wiki/.") from None
    if path.suffix != ".md":
        raise ValueError("Workflow target must be a markdown file.")
    return path


def _default_query_report_path(question: str) -> str:
    return f"scratch/reports/{date.today().isoformat()}-query-{_bounded_slug(question)}.md"


def _default_update_report_path(target_path: Path) -> str:
    return f"scratch/reports/{date.today().isoformat()}-workflow-update-{target_path.stem}.md"


def _default_workflow_ingest_report_path(source_path: Path) -> str:
    return f"scratch/reports/{date.today().isoformat()}-workflow-ingest-{source_path.stem}.md"


def _render_query_report(question: str) -> str:
    return "\n".join(
        [
            f"# Query Report - {date.today().isoformat()}",
            "",
            "## Question",
            "",
            question,
            "",
            "## Answer Summary",
            "",
            "- Semantic answer synthesis required.",
            "",
            "## Pages Consulted",
            "",
            "- Not filled yet.",
            "",
            "## Sources Consulted",
            "",
            "- Not filled yet.",
            "",
            "## Confidence",
            "",
            "- Not assessed yet.",
            "",
            "## Contradictions",
            "",
            "- Not checked yet.",
            "",
            "## Reusable Capture Recommendation",
            "",
            "- Not assessed yet.",
            "",
        ]
    )


def _render_query_capture_report(query_input: QueryCaptureInput) -> str:
    lines = [
        f"# Query Report - {date.today().isoformat()}",
        "",
        "## Question",
        "",
        query_input.question,
        "",
        "## Mode",
        "",
        f"Mode: {query_input.mode}",
        "",
        "## Answer Summary",
        "",
        query_input.answer_summary or "- Semantic answer synthesis required.",
        "",
        "## Pages Consulted",
        "",
    ]
    lines.extend([f"- `{page}`" for page in query_input.consulted_pages] or ["- None recorded."])
    lines.extend(["", "## Sources Consulted", ""])
    lines.extend([f"- `{source}`" for source in query_input.consulted_sources] or ["- None recorded."])
    lines.extend(["", "## Confidence", "", f"- {query_input.confidence}", "", "## Contradictions", ""])
    lines.extend([f"- {item}" for item in query_input.contradictions] or ["- None recorded."])
    lines.extend(
        [
            "",
            "## Reusable Capture Recommendation",
            "",
            query_input.capture_recommendation or "- Not assessed.",
            "",
        ]
    )
    return "\n".join(lines)


def _query_capture_draft(root: Path, query_input: QueryCaptureInput) -> dict[str, object]:
    target, target_link = _normalize_capture_target(root, query_input.capture_target, query_input.question)
    title = query_input.capture_title or _title_from_question(query_input.question)
    summary = query_input.answer_summary or f"Reusable answer capture for: {query_input.question}"
    related = [
        _wiki_target_from_path(page)
        for page in query_input.consulted_pages
        if page.startswith("wiki/") and page.endswith(".md")
    ]
    body = "\n".join(
        [
            f"# {title}",
            "",
            "## Question",
            "",
            query_input.question,
            "",
            "## Answer Summary",
            "",
            query_input.answer_summary or "Semantic answer synthesis required.",
            "",
            "## Consulted Pages",
            "",
            *([f"- `{page}`" for page in query_input.consulted_pages] or ["- None recorded."]),
            "",
            "## Consulted Sources",
            "",
            *([f"- `{source}`" for source in query_input.consulted_sources] or ["- None recorded."]),
            "",
            "## Confidence",
            "",
            query_input.confidence,
            "",
            "## Contradictions",
            "",
            *([f"- {item}" for item in query_input.contradictions] or ["- None recorded."]),
            "",
            "## Capture Recommendation",
            "",
            query_input.capture_recommendation or "Not assessed.",
            "",
            "Source: Query workflow capture.",
        ]
    )
    return {
        "version": 1,
        "operation": "upsert-page",
        "path": target,
        "frontmatter": {
            "title": title,
            "type": "question",
            "status": "active",
            "created": date.today().isoformat(),
            "updated": date.today().isoformat(),
            "owner": "agent",
            "summary": summary,
            "source_count": len(set(query_input.consulted_pages + query_input.consulted_sources)),
            "tags": ["query-capture"],
            "related": related,
            "confidence": query_input.confidence,
            "quality": {
                "provenance": "partial",
                "links": "unchecked",
                "contradictions": "unchecked" if not query_input.contradictions else "present",
                "review_required": bool(query_input.contradictions),
            },
        },
        "body": body,
        "index": {
            "section": "Questions",
            "target": target_link,
            "summary": summary,
        },
        "log": {
            "event_type": "query",
            "title": title,
            "items": [f"Captured reusable query answer as [[{target_link}]]."],
        },
    }


def _normalize_capture_target(root: Path, raw_target: str | None, question: str) -> tuple[str, str]:
    target = raw_target.strip() if raw_target else f"questions/{_bounded_slug(question)}"
    target_path = Path(target)
    if target_path.is_absolute():
        raise ValueError("Capture target must be relative to wiki/.")
    if any(part in {".", ".."} for part in target_path.parts):
        raise ValueError("Capture target must not contain '.' or '..' path segments.")
    if target.startswith("wiki/"):
        repo_relative = target_path
    else:
        repo_relative = Path("wiki") / target_path
    if repo_relative.suffix != ".md":
        repo_relative = repo_relative.with_suffix(".md")

    wiki_root = (root / "wiki").resolve()
    resolved_target = (root / repo_relative).resolve(strict=False)
    try:
        relative_to_wiki = resolved_target.relative_to(wiki_root)
    except ValueError:
        raise ValueError("Capture target must stay under wiki/.") from None
    target_link = relative_to_wiki.with_suffix("").as_posix()
    return (Path("wiki") / relative_to_wiki).as_posix(), target_link


def _render_update_preflight_report(
    target_path: Path,
    root: Path,
    page_locked: bool,
    locked_ranges: int,
    related: list[str],
    body_links: list[str],
    index_state: str,
    log_state: str,
) -> str:
    lines = [
        f"# Update Preflight Report - {date.today().isoformat()}",
        "",
        "## Summary",
        "",
        f"- Target page: `{display_path(target_path, root)}`",
        f"- Page locked: {page_locked}",
        f"- Section-level locked ranges: {locked_ranges}",
        f"- Index state: {index_state}",
        f"- Log state: {log_state}",
        "- Semantic update draft required.",
        "",
        "## Related Frontmatter",
        "",
    ]
    lines.extend([f"- `{item}`" for item in related] or ["- None."])
    lines.extend(["", "## Body Links", ""])
    lines.extend([f"- `[[{item}]]`" for item in body_links] or ["- None."])
    lines.extend(
        [
            "",
            "## Next Step",
            "",
            "- Agent must inspect evidence, preserve human locks, and publish a draft only through `publish-draft`.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_ingest_workflow_report(
    source_page: Path,
    source_path: Path,
    raw_hash: str,
    outcome: str,
) -> str:
    return "\n".join(
        [
            f"# Workflow Ingest Report - {date.today().isoformat()}",
            "",
            "## Summary",
            "",
            f"- Outcome: {outcome}",
            f"- Raw source: `{source_path}`",
            f"- Source page: `{source_page}`",
            f"- SHA-256: `{raw_hash}`",
            "- Semantic extraction pending.",
            "",
            "## Next Step",
            "",
            "- Agent must read the source and create semantic drafts for reusable concepts, systems, workflows, decisions, or claims.",
            "",
        ]
    )


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "query"


def _bounded_slug(text: str, max_length: int = 80) -> str:
    slug = _slugify(text)
    if len(slug) <= max_length:
        return slug
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    prefix_length = max_length - len(digest) - 1
    return f"{slug[:prefix_length].rstrip('-')}-{digest}"


def _default_query_draft_path(query_input: QueryCaptureInput) -> str:
    return f"scratch/drafts/{date.today().isoformat()}-query-{_bounded_slug(query_input.question)}.json"


def _title_from_question(question: str) -> str:
    stripped = question.strip().rstrip("?")
    return stripped[:1].upper() + stripped[1:] if stripped else "Query Capture"


def _validate_query_mode(mode: str) -> None:
    if mode not in {"answer-only", "answer-with-report", "answer-and-capture"}:
        raise ValueError("Query mode must be answer-only, answer-with-report, or answer-and-capture.")


def _validate_confidence(confidence: str) -> None:
    if confidence not in {"low", "medium", "high"}:
        raise ValueError("Query confidence must be low, medium, or high.")


def _validate_consulted_pages(root: Path, pages: list[str]) -> None:
    wiki_root = (root / "wiki").resolve()
    for page in pages:
        path = root / page
        try:
            path.resolve().relative_to(wiki_root)
        except ValueError:
            raise ValueError(f"Consulted page must be under wiki/: {page}") from None
        if not path.exists():
            raise ValueError(f"Consulted page does not exist: {page}")


def _validate_consulted_sources(root: Path, sources: list[str]) -> None:
    for source in sources:
        if source.startswith("raw/"):
            path = root / source
            raw_root = (root / "raw").resolve()
            try:
                path.resolve().relative_to(raw_root)
            except ValueError:
                raise ValueError(f"Consulted raw source must stay under raw/: {source}") from None
            if not path.exists():
                raise ValueError(f"Consulted raw source does not exist: {source}")
        elif source.startswith("wiki/"):
            path = root / source
            wiki_root = (root / "wiki").resolve()
            try:
                path.resolve().relative_to(wiki_root)
            except ValueError:
                raise ValueError(f"Consulted source page must stay under wiki/: {source}") from None
            if not path.exists():
                raise ValueError(f"Consulted source page does not exist: {source}")


def _wiki_target_from_path(path: str) -> str:
    target = path[len("wiki/") :] if path.startswith("wiki/") else path
    return target[:-3] if target.endswith(".md") else target


def _index_state(root: Path, target_path: Path) -> str:
    index_path = root / "wiki" / "index.md"
    if not index_path.exists():
        return "missing"
    target = target_path.relative_to(root / "wiki").with_suffix("").as_posix()
    return "linked" if f"[[{target}]]" in index_path.read_text() else "not-linked"


def _log_state(root: Path, target_path: Path) -> str:
    log_path = root / "wiki" / "log.md"
    if not log_path.exists():
        return "missing"
    target = target_path.relative_to(root / "wiki").with_suffix("").as_posix()
    text = log_path.read_text()
    return "mentioned" if target in text or target_path.name in text else "not-mentioned"
