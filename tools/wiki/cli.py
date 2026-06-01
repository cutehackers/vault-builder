#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.wiki.lib.frontmatter import parse_page
from tools.wiki.lib.draft import apply_batch_draft, apply_draft
from tools.wiki.lib.hash_source import compute_sha256, display_path
from tools.wiki.lib.ingest import ingest_source
from tools.wiki.lib.lint import (
    LintResult,
    lint_repo,
    render_lint_report,
    validate_human_locks,
    validate_lifecycle,
    validate_log,
    validate_page_links,
    validate_provenance,
    validate_source_hash,
)
from tools.wiki.lib.maps import build_navigation_maps
from tools.wiki.lib.merge import scan_merge_candidates
from tools.wiki.lib.metrics import build_maintenance_metrics
from tools.wiki.lib.paths import resolve_report_path
from tools.wiki.lib.review import create_review_item, list_review_items, render_review_list, resolve_review_item
from tools.wiki.lib.schema import Issue, validate_schema
from tools.wiki.lib.source_registry import (
    build_source_registry,
    bulk_ingest_sources,
    discover_raw_files,
    render_source_registry_report,
)
from tools.wiki.lib.workflow import (
    ensure_draft_targets_workflow_target,
    prepare_query_capture_workflow,
    prepare_ingest_workflow,
    prepare_query_workflow,
    prepare_update_workflow,
    QueryCaptureInput,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wiki")
    subcommands = parser.add_subparsers(dest="command", required=True)

    validate_page = subcommands.add_parser("validate-page")
    validate_page.add_argument("path")

    hash_source = subcommands.add_parser("hash-source")
    hash_source.add_argument("path")

    lint = subcommands.add_parser("lint")
    lint.add_argument("--report", nargs="?", const="__default__")

    health = subcommands.add_parser("health")

    metrics_cmd = subcommands.add_parser("metrics")
    metrics_cmd.add_argument("--report", nargs="?", const="__default__")
    metrics_cmd.add_argument("--check", action="store_true")
    metrics_cmd.add_argument("--policy")

    apply_draft_cmd = subcommands.add_parser("apply-draft")
    apply_draft_cmd.add_argument("path")
    apply_draft_cmd.add_argument("--report", nargs="?", const="__default__")
    apply_draft_cmd.add_argument("--dry-run", action="store_true")
    apply_draft_cmd.add_argument("--unsafe-write", action="store_true")

    publish_draft_cmd = subcommands.add_parser("publish-draft")
    publish_draft_cmd.add_argument("path")
    publish_draft_cmd.add_argument("--report", nargs="?", const="__default__")

    publish_batch_cmd = subcommands.add_parser("publish-batch")
    publish_batch_cmd.add_argument("path")
    publish_batch_cmd.add_argument("--report", nargs="?", const="__default__")
    publish_batch_cmd.add_argument("--dry-run", action="store_true")

    ingest_source_cmd = subcommands.add_parser("ingest-source")
    ingest_source_cmd.add_argument("path")
    ingest_source_cmd.add_argument("--title")
    ingest_source_cmd.add_argument("--summary")
    ingest_source_cmd.add_argument("--report", nargs="?", const="__default__")

    source_registry_cmd = subcommands.add_parser("source-registry")
    source_registry_cmd.add_argument("--report", nargs="?", const="__default__")

    bulk_ingest_cmd = subcommands.add_parser("bulk-ingest")
    bulk_ingest_cmd.add_argument("paths", nargs="*")
    bulk_ingest_cmd.add_argument("--all-raw", action="store_true")
    bulk_ingest_cmd.add_argument("--report", nargs="?", const="__default__")

    review_cmd = subcommands.add_parser("review")
    review_subcommands = review_cmd.add_subparsers(dest="review_command", required=True)

    review_create = review_subcommands.add_parser("create")
    review_create.add_argument("--type", required=True)
    review_create.add_argument("--summary", required=True)
    review_create.add_argument("--context", default="")
    review_create.add_argument("--related", action="append", default=[])
    review_create.add_argument("--evidence", action="append", default=[])

    review_list = review_subcommands.add_parser("list")
    review_list.add_argument("--status")

    review_resolve = review_subcommands.add_parser("resolve")
    review_resolve.add_argument("path")
    review_resolve.add_argument("--status", required=True)
    review_resolve.add_argument("--resolution", required=True)

    merge_cmd = subcommands.add_parser("merge")
    merge_subcommands = merge_cmd.add_subparsers(dest="merge_command", required=True)
    merge_scan = merge_subcommands.add_parser("scan")
    merge_scan.add_argument("--report", nargs="?", const="__default__")
    merge_scan.add_argument("--create-review", action="store_true")

    maps_cmd = subcommands.add_parser("maps")
    maps_subcommands = maps_cmd.add_subparsers(dest="maps_command", required=True)
    maps_build = maps_subcommands.add_parser("build")
    maps_build.add_argument("--report", nargs="?", const="__default__")
    maps_build.add_argument("--dry-run", action="store_true")
    maps_build.add_argument("--check", action="store_true")

    workflow_cmd = subcommands.add_parser("workflow")
    workflow_subcommands = workflow_cmd.add_subparsers(dest="workflow_command", required=True)

    workflow_ingest = workflow_subcommands.add_parser("ingest")
    workflow_ingest.add_argument("--source", required=True)
    workflow_ingest.add_argument("--title")
    workflow_ingest.add_argument("--summary")
    workflow_ingest.add_argument("--report", nargs="?", const="__default__")

    workflow_update = workflow_subcommands.add_parser("update")
    workflow_update.add_argument("--target", required=True)
    workflow_update.add_argument("--preflight", action="store_true")
    workflow_update.add_argument("--draft")
    workflow_update.add_argument("--publish", action="store_true")
    workflow_update.add_argument("--report", nargs="?", const="__default__")

    workflow_query = workflow_subcommands.add_parser("query")
    workflow_query.add_argument("--question")
    workflow_query.add_argument("--prepare-report", action="store_true")
    workflow_query.add_argument("--mode", choices=["answer-only", "answer-with-report", "answer-and-capture"])
    workflow_query.add_argument("--answer-summary", default="")
    workflow_query.add_argument("--consulted-page", action="append", default=[])
    workflow_query.add_argument("--consulted-source", action="append", default=[])
    workflow_query.add_argument("--confidence", choices=["low", "medium", "high"], default="medium")
    workflow_query.add_argument("--contradiction", action="append", default=[])
    workflow_query.add_argument("--capture-recommendation", default="")
    workflow_query.add_argument("--capture-target")
    workflow_query.add_argument("--capture-title")
    workflow_query.add_argument("--draft")
    workflow_query.add_argument("--publish", action="store_true")
    workflow_query.add_argument("--report", nargs="?", const="__default__")

    args = parser.parse_args(argv)

    if args.command == "validate-page":
        page = parse_page(Path(args.path))
        issues = validate_schema(page)
        issues.extend(validate_provenance(page, ROOT))
        issues.extend(validate_source_hash(page, ROOT))
        issues.extend(validate_lifecycle(page, ROOT / "wiki"))
        issues.extend(validate_human_locks(page))
        if page.path.name == "log.md":
            issues.extend(validate_log(page))
        issues.extend(validate_page_links(page, ROOT / "wiki"))
        _print_result(LintResult(issues=issues))
        return 0 if not issues else 1

    if args.command == "lint":
        result = lint_repo(ROOT)
        _print_result(result)
        if args.report is not None:
            try:
                report_path = resolve_report_path(ROOT, _default_report_path() if args.report == "__default__" else args.report)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(render_lint_report(result))
            print(f"Report written: {report_path}")
        return 0 if result.ok else 1

    if args.command == "health":
        result = lint_repo(ROOT)
        _print_result(result)
        report_path = resolve_report_path(ROOT, _default_report_path())
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_lint_report(result))
        print(f"Report written: {display_path(report_path, ROOT)}")
        return 0 if result.ok else 1

    if args.command == "metrics":
        report_path = None
        if args.report == "__default__":
            report_path = Path(_default_metrics_report_path())
        elif args.report:
            report_path = Path(args.report)
        policy_path = _metrics_policy_path(args.policy)
        try:
            result = build_maintenance_metrics(ROOT, report_path=report_path)
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        metrics = result.metrics
        print("Maintenance metrics:")
        print(f"Pending reviews: {metrics.pending_reviews}")
        print(f"Contested claim rows: {metrics.contested_claim_rows}")
        print(f"Stale sources: {metrics.stale_sources}")
        print(f"Orphan pages: {metrics.orphan_pages}")
        if result.report_path is not None:
            print(f"Report written: {display_path(result.report_path, ROOT)}")
        if args.check:
            try:
                if _metrics_has_actionable_signals(metrics, policy_path=policy_path):
                    print("Metrics check failed: maintenance signals require attention.")
                    return 1
            except (OSError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
        return 0

    if args.command == "hash-source":
        source_path = Path(args.path)
        if not source_path.is_absolute():
            source_path = ROOT / source_path
        digest = compute_sha256(source_path)
        print(f"{digest}  {display_path(source_path, ROOT)}")
        return 0

    if args.command == "apply-draft":
        if not args.dry_run and not args.unsafe_write:
            print(
                "apply-draft is validation-only by default; use publish-draft for durable writes "
                "or pass --unsafe-write for low-level debugging.",
                file=sys.stderr,
            )
            return 1
        draft_path = Path(args.path)
        if not draft_path.is_absolute():
            draft_path = ROOT / draft_path
        report_path = None
        if args.report == "__default__":
            report_path = Path(_default_draft_report_path(draft_path))
        elif args.report:
            report_path = Path(args.report)
        try:
            result = apply_draft(ROOT, draft_path, report_path=report_path, dry_run=args.dry_run)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        action = "Validated" if args.dry_run else "Applied"
        print(f"{action} draft: {display_path(result.target_path, ROOT)}")
        if result.report_path is not None:
            print(f"Report written: {display_path(result.report_path, ROOT)}")
        return 0

    if args.command == "publish-draft":
        draft_path = Path(args.path)
        if not draft_path.is_absolute():
            draft_path = ROOT / draft_path
        report_path = None
        if args.report == "__default__":
            report_path = Path(_default_publish_report_path(draft_path))
        elif args.report:
            report_path = Path(args.report)
        return _publish_draft(draft_path, report_path)

    if args.command == "publish-batch":
        draft_path = Path(args.path)
        if not draft_path.is_absolute():
            draft_path = ROOT / draft_path
        report_path = None
        if args.report == "__default__":
            report_path = Path(_default_publish_batch_report_path(draft_path))
        elif args.report:
            report_path = Path(args.report)
        return _publish_batch(draft_path, report_path, dry_run=args.dry_run)

    if args.command == "workflow":
        return _handle_workflow(args)

    if args.command == "review":
        return _handle_review(args)

    if args.command == "merge":
        return _handle_merge(args)

    if args.command == "maps":
        return _handle_maps(args)

    if args.command == "source-registry":
        entries = build_source_registry(ROOT)
        print(f"Source registry entries: {len(entries)}")
        for status, count in sorted(_count_entry_statuses(entries).items()):
            print(f"{status}: {count}")
        report_path = None
        if args.report == "__default__":
            report_path = Path(_default_source_registry_report_path())
        elif args.report:
            report_path = Path(args.report)
        if report_path is not None:
            try:
                report_path = resolve_report_path(ROOT, report_path)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(render_source_registry_report(entries))
            print(f"Report written: {display_path(report_path, ROOT)}")
        return 0

    if args.command == "bulk-ingest":
        source_paths = [Path(path) for path in args.paths]
        if args.all_raw:
            source_paths.extend(discover_raw_files(ROOT))
        if not source_paths and not args.all_raw:
            print("bulk-ingest requires at least one path or --all-raw.", file=sys.stderr)
            return 1
        report_path = None
        if args.report == "__default__":
            report_path = Path(_default_bulk_ingest_report_path())
        elif args.report:
            report_path = Path(args.report)
        result = bulk_ingest_sources(ROOT, source_paths, report_path=report_path)
        print("Bulk ingest completed.")
        for outcome, count in sorted(result.counts_by_outcome().items()):
            print(f"{outcome}: {count}")
        if result.report_path is not None:
            print(f"Report written: {display_path(result.report_path, ROOT)}")
        return 0 if result.ok else 1

    if args.command == "ingest-source":
        source_path = Path(args.path)
        if not source_path.is_absolute():
            source_path = ROOT / source_path
        report_path = None
        if args.report == "__default__":
            report_path = Path(_default_ingest_report_path(source_path))
        elif args.report:
            report_path = Path(args.report)
        try:
            result = ingest_source(
                ROOT,
                source_path,
                title=args.title,
                summary=args.summary,
                report_path=report_path,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Ingested source: {display_path(result.source_page, ROOT)}")
        print(f"outcome: {result.outcome}")
        print(f"raw_sha256: {result.raw_hash}")
        if result.report_path is not None:
            print(f"Report written: {display_path(result.report_path, ROOT)}")
        return 0

    return 2


def _handle_review(args: argparse.Namespace) -> int:
    try:
        if args.review_command == "create":
            item = create_review_item(
                ROOT,
                review_type=args.type,
                summary=args.summary,
                context=args.context,
                related_pages=args.related,
                evidence=args.evidence,
            )
            print(f"Review item created: {display_path(item.path, ROOT)}")
            return 0

        if args.review_command == "list":
            items = list_review_items(ROOT)
            if args.status:
                items = [item for item in items if item.status == args.status]
            print(render_review_list(ROOT, items), end="")
            return 0

        if args.review_command == "resolve":
            return _resolve_review_with_gate(Path(args.path), status=args.status, resolution=args.resolution)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 2


def _resolve_review_with_gate(review_path: Path, status: str, resolution: str) -> int:
    if not review_path.is_absolute():
        review_path = ROOT / review_path
    snapshot = _snapshot_review_resolve_paths(review_path)

    try:
        pre_lint_result = lint_repo(ROOT)
        if not pre_lint_result.ok:
            _print_result(pre_lint_result)
            lint_report_path = _write_lint_report(pre_lint_result)
            print(f"Report written: {display_path(lint_report_path, ROOT)}")
            print("Review resolution aborted: wiki lint must be clean before resolving a review item.")
            return 1

        item = resolve_review_item(ROOT, review_path, status=status, resolution=resolution)
        lint_result = lint_repo(ROOT)
        if not lint_result.ok:
            _print_result(lint_result)
            lint_report_path = _write_lint_report(lint_result)
            _restore_snapshot(snapshot)
            print(f"Report written: {display_path(lint_report_path, ROOT)}")
            print("Rolled back review resolution: lint failed after resolving.")
            return 1
    except (OSError, ValueError):
        _restore_snapshot(snapshot)
        raise

    print(f"Review item resolved: {display_path(item.path, ROOT)}")
    return 0


def _handle_merge(args: argparse.Namespace) -> int:
    try:
        if args.merge_command == "scan":
            report_path = None
            if args.report == "__default__":
                report_path = Path(_default_merge_report_path())
            elif args.report:
                report_path = Path(args.report)
            result = scan_merge_candidates(ROOT, report_path=report_path, create_review=args.create_review)
            print(f"Merge candidates: {len(result.candidates)}")
            if result.report_path is not None:
                print(f"Report written: {display_path(result.report_path, ROOT)}")
            if result.review_path is not None:
                print(f"Review item created: {display_path(result.review_path, ROOT)}")
            return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 2


def _handle_maps(args: argparse.Namespace) -> int:
    try:
        if args.maps_command == "build":
            if args.dry_run and args.check:
                raise ValueError("maps build accepts either --dry-run or --check, not both.")
            report_path = None
            if args.report == "__default__":
                report_path = Path(_default_maps_report_path())
            elif args.report:
                report_path = Path(args.report)
            result = build_navigation_maps(
                ROOT,
                report_path=report_path,
                dry_run=args.dry_run,
                check=args.check,
            )
            action = "Checked" if args.check else "Validated" if args.dry_run else "Built"
            print(f"{action} navigation maps.")
            print(f"Navigation maps: {len(result.target_paths)}")
            print(f"Changed maps: {len(result.changed_paths)}")
            if result.report_path is not None:
                print(f"Report written: {display_path(result.report_path, ROOT)}")
            if args.check and result.changed_paths:
                print("Map check failed: generated maps are stale.")
                return 1
            return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 2


def _handle_workflow(args: argparse.Namespace) -> int:
    try:
        if args.workflow_command == "ingest":
            source_path = Path(args.source)
            if not source_path.is_absolute():
                source_path = ROOT / source_path
            report_path = _optional_report_path(args.report)
            result = prepare_ingest_workflow(
                ROOT,
                source_path,
                report_path=report_path,
                title=args.title,
                summary=args.summary,
            )
            print(result.message)
            return 0

        if args.workflow_command == "update":
            target_path = Path(args.target)
            if args.preflight:
                result = prepare_update_workflow(ROOT, target_path, report_path=_optional_report_path(args.report))
                print(result.message)
                return 0
            if args.publish:
                if not args.draft:
                    raise ValueError("workflow update --publish requires --draft.")
                draft_path = Path(args.draft)
                if not draft_path.is_absolute():
                    draft_path = ROOT / draft_path
                ensure_draft_targets_workflow_target(ROOT, draft_path, target_path)
                return _publish_draft(draft_path, _publish_report_path(args.report, draft_path))
            raise ValueError("workflow update requires --preflight or --publish.")

        if args.workflow_command == "query":
            if args.mode:
                if not args.question:
                    raise ValueError("workflow query --mode requires --question.")
                result = prepare_query_capture_workflow(
                    ROOT,
                    QueryCaptureInput(
                        question=args.question,
                        mode=args.mode,
                        answer_summary=args.answer_summary,
                        consulted_pages=args.consulted_page,
                        consulted_sources=args.consulted_source,
                        confidence=args.confidence,
                        contradictions=args.contradiction,
                        capture_recommendation=args.capture_recommendation,
                        capture_target=args.capture_target,
                        capture_title=args.capture_title,
                    ),
                    report_path=_optional_report_path(args.report),
                    draft_path=Path(args.draft) if args.draft else None,
                )
                print(result.message)
                return 0
            if args.prepare_report:
                if not args.question:
                    raise ValueError("workflow query --prepare-report requires --question.")
                result = prepare_query_workflow(ROOT, args.question, report_path=_optional_report_path(args.report))
                print(result.message)
                return 0
            if args.publish:
                if not args.draft:
                    raise ValueError("workflow query --publish requires --draft.")
                draft_path = Path(args.draft)
                if not draft_path.is_absolute():
                    draft_path = ROOT / draft_path
                return _publish_draft(draft_path, _publish_report_path(args.report, draft_path))
            raise ValueError("workflow query requires --prepare-report or --publish.")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 2


def _optional_report_path(raw_report: str | None) -> Path | None:
    if raw_report is None or raw_report == "__default__":
        return None
    return Path(raw_report)


def _publish_report_path(raw_report: str | None, draft_path: Path) -> Path | None:
    if raw_report == "__default__":
        return Path(_default_publish_report_path(draft_path))
    if raw_report:
        return Path(raw_report)
    return None


def _publish_draft(draft_path: Path, report_path: Path | None) -> int:
    snapshot = None
    try:
        pre_lint_result = lint_repo(ROOT)
        if not pre_lint_result.ok:
            _print_result(pre_lint_result)
            lint_report_path = _write_lint_report(pre_lint_result)
            print(f"Report written: {display_path(lint_report_path, ROOT)}")
            print("Publish aborted: wiki lint must be clean before applying a draft.")
            return 1
        dry_run_result = apply_draft(ROOT, draft_path, dry_run=True)
        print(f"Validated draft: {display_path(dry_run_result.target_path, ROOT)}")
        snapshot = _snapshot_publish_paths(dry_run_result.target_path)
        applied_result = apply_draft(ROOT, draft_path, dry_run=False)
        print(f"Applied draft: {display_path(applied_result.target_path, ROOT)}")
    except (OSError, ValueError) as exc:
        if snapshot is not None:
            _restore_snapshot(snapshot)
        print(str(exc), file=sys.stderr)
        return 1

    lint_result = lint_repo(ROOT)
    _print_result(lint_result)
    lint_report_path = _write_lint_report(lint_result)
    print(f"Report written: {display_path(lint_report_path, ROOT)}")
    if not lint_result.ok:
        _restore_snapshot(snapshot)
        print("Rolled back draft: lint failed after applying.")
        return 1
    if report_path is not None:
        try:
            report_path = resolve_report_path(ROOT, report_path)
        except ValueError as exc:
            _restore_snapshot(snapshot)
            print(str(exc), file=sys.stderr)
            return 1
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_render_publish_report(draft_path, applied_result.target_path))
        print(f"Report written: {display_path(report_path, ROOT)}")
    return 0 if lint_result.ok else 1


def _publish_batch(draft_path: Path, report_path: Path | None, dry_run: bool = False) -> int:
    snapshot = None
    try:
        pre_lint_result = lint_repo(ROOT)
        if not pre_lint_result.ok:
            _print_result(pre_lint_result)
            lint_report_path = _write_lint_report(pre_lint_result)
            print(f"Report written: {display_path(lint_report_path, ROOT)}")
            print("Publish aborted: wiki lint must be clean before applying a batch draft.")
            return 1
        dry_run_result = apply_batch_draft(ROOT, draft_path, dry_run=True)
        print(f"Validated batch draft: {len(dry_run_result.target_paths)} pages")
        if dry_run:
            return 0
        snapshot = _snapshot_publish_paths_for_targets(dry_run_result.target_paths)
        applied_result = apply_batch_draft(ROOT, draft_path, dry_run=False)
        print(f"Applied batch draft: {len(applied_result.target_paths)} pages")
    except (OSError, ValueError) as exc:
        if snapshot is not None:
            _restore_snapshot(snapshot)
        print(str(exc), file=sys.stderr)
        return 1

    lint_result = lint_repo(ROOT)
    _print_result(lint_result)
    lint_report_path = _write_lint_report(lint_result)
    print(f"Report written: {display_path(lint_report_path, ROOT)}")
    if not lint_result.ok:
        _restore_snapshot(snapshot)
        print("Rolled back batch draft: lint failed after applying.")
        return 1
    if report_path is not None:
        try:
            report_path = resolve_report_path(ROOT, report_path)
        except ValueError as exc:
            _restore_snapshot(snapshot)
            print(str(exc), file=sys.stderr)
            return 1
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_render_publish_batch_report(draft_path, applied_result.target_paths))
        print(f"Report written: {display_path(report_path, ROOT)}")
    return 0


def _default_report_path() -> str:
    return f"scratch/reports/{date.today().isoformat()}-lint.md"


def _default_draft_report_path(draft_path: Path) -> str:
    return f"scratch/reports/{date.today().isoformat()}-apply-draft-{draft_path.stem}.md"


def _default_publish_report_path(draft_path: Path) -> str:
    return f"scratch/reports/{date.today().isoformat()}-publish-draft-{draft_path.stem}.md"


def _default_publish_batch_report_path(draft_path: Path) -> str:
    return f"scratch/reports/{date.today().isoformat()}-publish-batch-{draft_path.stem}.md"


def _default_ingest_report_path(source_path: Path) -> str:
    return f"scratch/reports/{date.today().isoformat()}-ingest-{source_path.stem}.md"


def _default_source_registry_report_path() -> str:
    return f"scratch/reports/{date.today().isoformat()}-source-registry.md"


def _default_bulk_ingest_report_path() -> str:
    return f"scratch/reports/{date.today().isoformat()}-bulk-ingest.md"


def _default_merge_report_path() -> str:
    return f"scratch/reports/{date.today().isoformat()}-merge-scan.md"


def _default_maps_report_path() -> str:
    return f"scratch/reports/{date.today().isoformat()}-maps-build.md"


def _default_metrics_report_path() -> str:
    return f"scratch/reports/{date.today().isoformat()}-metrics.md"


METRIC_CHECK_FIELDS = [
    "pending_reviews",
    "contested_claim_rows",
    "unreviewed_contested_claim_rows",
    "stale_sources",
    "unregistered_sources",
    "orphan_pages",
    "pages_without_claim_level_provenance",
    "deprecated_linked_pages",
]


def _count_entry_statuses(entries: list[object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        status = str(getattr(entry, "ingest_status"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _metrics_policy_path(raw_policy: str | None) -> Path | None:
    if raw_policy is None:
        return None
    policy_path = Path(raw_policy)
    if not policy_path.is_absolute():
        policy_path = ROOT / policy_path
    return policy_path


def _default_metrics_policy_path() -> Path:
    return ROOT / "tools" / "wiki" / "metrics-policy.json"


def _metrics_check_thresholds(policy_path: Path | None = None) -> dict[str, int]:
    thresholds = {field: 0 for field in METRIC_CHECK_FIELDS}
    candidate = policy_path or _default_metrics_policy_path()
    if not candidate.exists():
        if policy_path is not None:
            raise ValueError(f"Metrics policy does not exist: {display_path(candidate, ROOT)}")
        return thresholds

    data = json.loads(candidate.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Metrics policy must be a JSON object: {display_path(candidate, ROOT)}")
    max_values = data.get("max", {})
    if not isinstance(max_values, dict):
        raise ValueError(f"Metrics policy max must be an object: {display_path(candidate, ROOT)}")

    for field in METRIC_CHECK_FIELDS:
        if field in max_values:
            value = max_values[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"Metrics policy max.{field} must be a non-negative integer: {display_path(candidate, ROOT)}"
                )
            thresholds[field] = value
    return thresholds


def _metrics_has_actionable_signals(metrics: object, policy_path: Path | None = None) -> bool:
    thresholds = _metrics_check_thresholds(policy_path)
    return any(int(getattr(metrics, field, 0)) > thresholds[field] for field in METRIC_CHECK_FIELDS)


def _render_publish_report(draft_path: Path, target_path: Path) -> str:
    return "\n".join(
        [
            f"# Publish Draft Report - {date.today().isoformat()}",
            "",
            "## Summary",
            "",
            f"- Published draft: `{display_path(draft_path, ROOT)}`",
            f"- Target page: `{display_path(target_path, ROOT)}`",
            "- Dry-run validation completed before applying.",
            "- Lint report written after applying.",
            "",
        ]
    )


def _render_publish_batch_report(draft_path: Path, target_paths: list[Path]) -> str:
    lines = [
        f"# Publish Batch Report - {date.today().isoformat()}",
        "",
        "## Summary",
        "",
        f"- Published batch draft: `{display_path(draft_path, ROOT)}`",
        f"- Target pages: {len(target_paths)}",
        "- Dry-run validation completed before applying.",
        "- Lint report written after applying.",
        "",
        "## Manifest",
        "",
    ]
    lines.extend(f"- `{display_path(target_path, ROOT)}`" for target_path in target_paths)
    lines.append("")
    return "\n".join(lines)


def _write_lint_report(result: LintResult) -> Path:
    lint_report_path = resolve_report_path(ROOT, _default_report_path())
    lint_report_path.parent.mkdir(parents=True, exist_ok=True)
    lint_report_path.write_text(render_lint_report(result))
    return lint_report_path


def _snapshot_publish_paths(target_path: Path) -> dict[Path, str | None]:
    paths = {
        target_path,
        ROOT / "wiki" / "index.md",
        ROOT / "wiki" / "log.md",
    }
    return {path: path.read_text() if path.exists() else None for path in paths}


def _snapshot_publish_paths_for_targets(target_paths: list[Path]) -> dict[Path, str | None]:
    paths = set(target_paths)
    paths.add(ROOT / "wiki" / "index.md")
    paths.add(ROOT / "wiki" / "log.md")
    return {path: path.read_text() if path.exists() else None for path in paths}


def _snapshot_review_resolve_paths(review_path: Path) -> dict[Path, str | None]:
    paths = {
        review_path,
        ROOT / "wiki" / "log.md",
    }
    return {path: path.read_text() if path.exists() else None for path in paths}


def _restore_snapshot(snapshot: dict[Path, str | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _print_result(result: LintResult) -> None:
    if not result.issues:
        print("No issues found.")
        return

    print(f"Issues found: {len(result.issues)}")
    for issue in result.issues:
        print(_format_issue(issue))


def _format_issue(issue: Issue) -> str:
    location = f":{issue.line}" if issue.line is not None else ""
    return f"[{issue.severity}] {issue.code} {issue.path}{location} - {issue.message}"


if __name__ == "__main__":
    raise SystemExit(main())
