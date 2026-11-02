"""Publish one draft: lint gate, dry-run, apply, lint again, roll back on failure, rebuild maps.

`publish_draft` returns a `PublishResult` and prints nothing. The CLI prints the
result as text (`publish-draft`) or as the `vault-publish-result/v1` JSON object
(`publish-draft --json`). Other Vault commands call `publish_draft` directly.

Allowed imports inside `tools/wiki/lib`: draft, lint, maps (spec 4.4).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .draft import HumanLockError, PatchTargetMissing, apply_draft, resolve_report_path
from .lint import LintResult, lint_repo, render_lint_report
from .maps import build_navigation_maps

PUBLISH_RESULT_FORMAT = "vault-publish-result/v1"

STATUS_PUBLISHED = "published"
STATUS_REJECTED = "rejected"

REASON_LINT_DIRTY_BEFORE = "lint-dirty-before"
REASON_DRAFT_INVALID = "draft-invalid"
REASON_HUMAN_LOCKED = "human-locked"
REASON_PAGE_MISSING = "page-missing"
REASON_LINT_FAILED_AFTER = "lint-failed-after"
REASON_IO_ERROR = "io-error"

STDOUT = "stdout"
STDERR = "stderr"


@dataclass(frozen=True)
class PublishLine:
    """One human-readable line and the stream the text CLI prints it to."""

    stream: str
    text: str


@dataclass(frozen=True)
class PublishResult:
    status: str
    reason: str | None
    draft_version: int | None
    written_paths: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    lines: list[PublishLine] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.status == STATUS_PUBLISHED else 1

    def to_json(self) -> dict[str, Any]:
        return {
            "format": PUBLISH_RESULT_FORMAT,
            "draft_version": self.draft_version,
            "status": self.status,
            "reason": self.reason,
            "written_paths": list(self.written_paths),
            "messages": list(self.messages),
        }


def publish_draft(root: Path, draft_path: Path, report_path: Path | None = None) -> PublishResult:
    """Publish `draft_path` into the Vault at `root`.

    `written_paths` lists Vault-relative files the publish changed: the target page,
    `wiki/index.md` and `wiki/log.md` when their text changed, the publish report, and
    rebuilt map pages. A rejected publish leaves every one of them as it was, so its
    `written_paths` is empty.
    """
    run = _PublishRun(root, draft_path)
    return run.publish(report_path)


class _PublishRun:
    def __init__(self, root: Path, draft_path: Path) -> None:
        self.root = root
        self.draft_path = draft_path
        self.draft_version = _read_draft_version(draft_path)
        self.lines: list[PublishLine] = []

    def out(self, text: str) -> None:
        self.lines.append(PublishLine(STDOUT, text))

    def err(self, text: str) -> None:
        self.lines.append(PublishLine(STDERR, text))

    def rejected(self, reason: str, messages: list[str]) -> PublishResult:
        return PublishResult(
            status=STATUS_REJECTED,
            reason=reason,
            draft_version=self.draft_version,
            messages=messages,
            lines=self.lines,
        )

    def publish(self, report_path: Path | None) -> PublishResult:
        try:
            pre_lint_result = lint_repo(self.root)
            if not pre_lint_result.ok:
                self.print_lint(pre_lint_result)
                self.out(f"Report written: {self.display(self.write_lint_report(pre_lint_result))}")
                self.out("Publish aborted: wiki lint must be clean before applying a draft.")
                return self.rejected(REASON_LINT_DIRTY_BEFORE, _issue_messages(pre_lint_result))
        except OSError as exc:
            self.err(str(exc))
            return self.rejected(REASON_IO_ERROR, [str(exc)])
        except ValueError as exc:
            self.err(str(exc))
            return self.rejected(REASON_LINT_DIRTY_BEFORE, [str(exc)])

        snapshot: dict[Path, str | None] | None = None
        try:
            dry_run_result = apply_draft(self.root, self.draft_path, dry_run=True)
            self.out(f"Validated draft: {self.display(dry_run_result.target_path)}")
            snapshot = self.snapshot(dry_run_result.target_path)
            applied_result = apply_draft(self.root, self.draft_path, dry_run=False)
            self.out(f"Applied draft: {self.display(applied_result.target_path)}")
        except (OSError, ValueError) as exc:
            if snapshot is not None:
                _restore_snapshot(snapshot)
            self.err(str(exc))
            return self.rejected(_apply_error_reason(exc), [str(exc)])

        try:
            lint_result = lint_repo(self.root)
            self.print_lint(lint_result)
            self.out(f"Report written: {self.display(self.write_lint_report(lint_result))}")
        except (OSError, ValueError) as exc:
            _restore_snapshot(snapshot)
            self.err(str(exc))
            return self.rejected(REASON_IO_ERROR, [str(exc)])
        if not lint_result.ok:
            _restore_snapshot(snapshot)
            self.out("Rolled back draft: lint failed after applying.")
            return self.rejected(REASON_LINT_FAILED_AFTER, _issue_messages(lint_result))

        written = [self.display(applied_result.target_path)]
        written.extend(
            self.display(path)
            for path, before in snapshot.items()
            if path != applied_result.target_path and _read_or_none(path) != before
        )
        if report_path is not None:
            try:
                report_path = resolve_report_path(self.root, report_path)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(self.render_report(applied_result.target_path))
            except (OSError, ValueError) as exc:
                _restore_snapshot(snapshot)
                self.err(str(exc))
                return self.rejected(REASON_IO_ERROR, [str(exc)])
            self.out(f"Report written: {self.display(report_path)}")
            written.append(self.display(report_path))
        written.extend(self.rebuild_maps())

        return PublishResult(
            status=STATUS_PUBLISHED,
            reason=None,
            draft_version=self.draft_version,
            written_paths=_unique(written),
            messages=[],
            lines=self.lines,
        )

    def rebuild_maps(self) -> list[str]:
        """Keep generated maps current so a published page does not leave the release gate failing."""
        try:
            result = build_navigation_maps(self.root)
        except Exception as exc:  # The draft is already published; report instead of failing the command.
            self.err(f"Navigation maps not rebuilt: {exc}. Run maps build before the release gate.")
            return []
        self.out(f"Rebuilt navigation maps: {len(result.changed_paths)} changed.")
        return [self.display(path) for path in result.changed_paths]

    def print_lint(self, result: LintResult) -> None:
        if not result.issues:
            self.out("No issues found.")
            return
        self.out(f"Issues found: {len(result.issues)}")
        for issue in result.issues:
            self.out(_format_issue(issue))

    def write_lint_report(self, result: LintResult) -> Path:
        lint_report_path = resolve_report_path(self.root, f"scratch/reports/{date.today().isoformat()}-lint.md")
        lint_report_path.parent.mkdir(parents=True, exist_ok=True)
        lint_report_path.write_text(render_lint_report(result))
        return lint_report_path

    def snapshot(self, target_path: Path) -> dict[Path, str | None]:
        paths = [target_path, self.root / "wiki" / "index.md", self.root / "wiki" / "log.md"]
        return {path: _read_or_none(path) for path in paths}

    def render_report(self, target_path: Path) -> str:
        return "\n".join(
            [
                f"# Publish Draft Report - {date.today().isoformat()}",
                "",
                "## Summary",
                "",
                f"- Published draft: `{self.display(self.draft_path)}`",
                f"- Target page: `{self.display(target_path)}`",
                "- Dry-run validation completed before applying.",
                "- Lint report written after applying.",
                "",
            ]
        )

    def display(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()


def _apply_error_reason(exc: Exception) -> str:
    if isinstance(exc, HumanLockError):
        return REASON_HUMAN_LOCKED
    if isinstance(exc, PatchTargetMissing):
        return REASON_PAGE_MISSING
    if isinstance(exc, OSError):
        return REASON_IO_ERROR
    return REASON_DRAFT_INVALID


def _read_draft_version(draft_path: Path) -> int | None:
    try:
        draft = json.loads(draft_path.read_text())
    except (OSError, ValueError):
        return None
    version = draft.get("version") if isinstance(draft, dict) else None
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    return version


def _read_or_none(path: Path) -> str | None:
    return path.read_text() if path.exists() else None


def _restore_snapshot(snapshot: dict[Path, str | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _issue_messages(result: LintResult) -> list[str]:
    return [_format_issue(issue) for issue in result.issues]


def _format_issue(issue: Any) -> str:
    location = f":{issue.line}" if issue.line is not None else ""
    return f"[{issue.severity}] {issue.code} {issue.path}{location} - {issue.message}"


def _unique(paths: list[str]) -> list[str]:
    return list(dict.fromkeys(paths))
