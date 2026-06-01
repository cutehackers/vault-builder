import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
import subprocess
import sys
from datetime import date
from pathlib import Path

from tools.wiki.lib.draft import apply_batch_draft, apply_draft
from tools.wiki.lib.frontmatter import parse_page, render_page
from tools.wiki.lib.ingest import ingest_source
from tools.wiki.lib.links import find_wiki_links, resolve_wiki_link
from tools.wiki.lib.lint import (
    find_human_locks,
    lint_repo,
    validate_human_locks,
    validate_log,
    validate_provenance,
)
from tools.wiki.lib.maps import build_navigation_maps
from tools.wiki.lib.schema import validate_schema


VALID_PAGE = """---
title: "Valid Page"
type: concept
status: active
created: 2026-05-26
updated: 2026-05-26
owner: agent
summary: "A valid page."
source_count: 0
tags: [quality]
related:
  - overview
confidence: high
quality:
  provenance: section
  links: unchecked
  contradictions: none
  review_required: false
---

# Valid Page

Source: Human instruction in `AGENTS.md`.
"""


def _minimal_vault(root: Path) -> None:
    (root / "raw" / "sources").mkdir(parents=True)
    (root / "raw" / "imports").mkdir(parents=True)
    (root / "wiki" / "sources").mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "scratch" / "drafts").mkdir(parents=True)
    (root / "scratch" / "reports").mkdir(parents=True)
    (root / "wiki" / "index.md").write_text(
        VALID_PAGE.replace("type: concept", "type: index").replace(
            "# Valid Page",
            "# Index\n\n## Sources\n\n## Concepts",
        )
    )
    (root / "wiki" / "log.md").write_text(
        VALID_PAGE.replace("type: concept", "type: log").replace(
            "# Valid Page",
            "# Log\n\n## [2026-05-26] repair | Initial\n\n- Initial log entry.",
        )
    )


def _frontmatter(title: str, page_type: str = "concept") -> dict:
    return {
        "title": title,
        "type": page_type,
        "status": "active",
        "created": "2026-05-30",
        "updated": "2026-05-30",
        "owner": "agent",
        "summary": f"{title} summary.",
        "source_count": 0,
        "tags": ["test"],
        "related": [],
        "confidence": "high",
        "quality": {
            "provenance": "none",
            "links": "unchecked",
            "contradictions": "none",
            "review_required": False,
        },
    }


def _draft(
    path: str,
    title: str,
    body: str,
    index_target: str | None = None,
) -> dict:
    draft = {
        "version": 1,
        "operation": "upsert-page",
        "path": path,
        "frontmatter": _frontmatter(title),
        "body": body,
    }
    if index_target is not None:
        draft["index"] = {
            "section": "Concepts",
            "target": index_target,
            "summary": f"{title} summary.",
        }
        draft["log"] = {
            "event_type": "repair",
            "title": title,
            "items": [f"Updated [[{index_target}]]."],
        }
    return draft


def _batch_page_draft(
    path: str,
    title: str,
    body: str,
    index_target: str | None = None,
) -> dict:
    draft = _draft(path, title, body, index_target)
    draft.pop("log", None)
    return draft


def _claim_page_text(source_count: int, body: str) -> str:
    return (
        VALID_PAGE.replace("source_count: 0", f"source_count: {source_count}")
        .replace("  provenance: section", "  provenance: claim")
        .replace("# Valid Page\n\nSource: Human instruction in `AGENTS.md`.", f"# Valid Page\n\n{body}")
    )


class WikiToolSchemaTest(unittest.TestCase):
    def test_parse_page_requires_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "missing.md"
            path.write_text("# Missing\n")

            page = parse_page(path)

        self.assertIsNone(page.frontmatter)
        self.assertEqual("missing.md", page.path.name)

    def test_validate_schema_accepts_valid_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "valid.md"
            path.write_text(VALID_PAGE)

            issues = validate_schema(parse_page(path))

        self.assertEqual([], issues)

    def test_validate_schema_reports_missing_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "missing.md"
            path.write_text("# Missing\n")

            issues = validate_schema(parse_page(path))

        self.assertEqual(["missing-frontmatter"], [issue.code for issue in issues])

    def test_validate_schema_reports_missing_quality_fields(self):
        text = VALID_PAGE.replace("  review_required: false\n", "")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad-quality.md"
            path.write_text(text)

            issues = validate_schema(parse_page(path))

        self.assertIn("missing-quality-field", [issue.code for issue in issues])

    def test_validate_schema_reports_invalid_enum_values(self):
        text = VALID_PAGE.replace("type: concept", "type: unknown")
        text = text.replace("confidence: high", "confidence: certain")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad-enum.md"
            path.write_text(text)

            issues = validate_schema(parse_page(path))

        self.assertEqual(["invalid-enum", "invalid-enum"], [issue.code for issue in issues])

    def test_validate_schema_reports_invalid_required_field_types(self):
        text = VALID_PAGE.replace("source_count: 0", "source_count: many")
        text = text.replace("tags: [quality]", "tags: quality")
        text = text.replace("  review_required: false", "  review_required: maybe")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad-types.md"
            path.write_text(text)

            issues = validate_schema(parse_page(path))

        self.assertEqual(
            ["invalid-field-type", "invalid-field-type", "invalid-field-type"],
            [issue.code for issue in issues],
        )

    def test_validate_schema_reports_invalid_frontmatter_without_crashing(self):
        text = """---
title "Missing colon"
type: concept
---

# Bad
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad-frontmatter.md"
            path.write_text(text)

            issues = validate_schema(parse_page(path))

        self.assertIn("invalid-frontmatter", [issue.code for issue in issues])


class WikiToolStructuralTest(unittest.TestCase):
    def test_find_wiki_links_extracts_obsidian_links(self):
        links = find_wiki_links("See [[overview]] and [[concepts/compiled-knowledge|compiled]].")

        self.assertEqual(["overview", "concepts/compiled-knowledge"], links)

    def test_resolve_wiki_link_supports_root_and_nested_pages(self):
        root = Path(__file__).resolve().parents[1]

        self.assertEqual(root / "wiki" / "overview.md", resolve_wiki_link("overview", root / "wiki"))
        self.assertEqual(
            root / "wiki" / "concepts" / "compiled-knowledge.md",
            resolve_wiki_link("concepts/compiled-knowledge", root / "wiki"),
        )

    def test_lint_repo_reports_broken_wiki_links(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wiki = root / "wiki"
            wiki.mkdir()
            (wiki / "index.md").write_text(VALID_PAGE.replace("# Valid Page", "# Index"))
            (wiki / "broken.md").write_text(
                VALID_PAGE.replace("# Valid Page", "# Broken").replace(
                    "Source: Human instruction in `AGENTS.md`.",
                    "Source: Human instruction in `AGENTS.md`.\n\nSee [[missing-page]].",
                )
            )

            result = lint_repo(root)

        self.assertIn("broken-link", [issue.code for issue in result.issues])

    def test_lint_repo_rejects_wiki_links_outside_wiki_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wiki = root / "wiki"
            wiki.mkdir()
            (root / "outside.md").write_text("# Outside\n")
            (wiki / "index.md").write_text(VALID_PAGE.replace("# Valid Page", "# Index"))
            (wiki / "escape.md").write_text(
                VALID_PAGE.replace("# Valid Page", "# Escape").replace(
                    "Source: Human instruction in `AGENTS.md`.",
                    "Source: Human instruction in `AGENTS.md`.\n\nSee [[../outside]].",
                )
            )

            result = lint_repo(root)

        self.assertIn("wiki-link-outside-root", [issue.code for issue in result.issues])

    def test_lint_repo_reports_orphan_durable_pages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wiki = root / "wiki"
            concepts = wiki / "concepts"
            concepts.mkdir(parents=True)
            (wiki / "index.md").write_text(
                VALID_PAGE.replace("type: concept", "type: index").replace(
                    "# Valid Page",
                    "# Index\n\n- [[overview]]\n- [[concepts/linked]]",
                )
            )
            (wiki / "overview.md").write_text(VALID_PAGE.replace("# Valid Page", "# Overview"))
            (concepts / "linked.md").write_text(VALID_PAGE.replace("# Valid Page", "# Linked"))
            (concepts / "orphan.md").write_text(VALID_PAGE.replace("# Valid Page", "# Orphan"))

            result = lint_repo(root)

        self.assertIn("orphan-page", [issue.code for issue in result.issues])

    def test_lint_repo_reports_invalid_review_item_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            review_path = root / "scratch" / "review" / "invalid-review.md"
            review_path.parent.mkdir(parents=True, exist_ok=True)
            review_path.write_text(
                "---\nreview_id: invalid-review\n"
                "type: contradiction\nstatus: unknown\ncreated: 2026-05-30\nupdated: 2026-05-30\n"
                "summary: Invalid Review\nrelated_pages: []\nevidence: []\nresolution: \"\"\n---\n\n# Invalid Review\n"
            )

            result = lint_repo(root)

        self.assertIn("invalid-review-status", [issue.code for issue in result.issues])

    def test_lint_repo_reports_stale_review_refs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            review_path = root / "scratch" / "review" / "stale-review.md"
            review_path.parent.mkdir(parents=True, exist_ok=True)
            review_path.write_text(
                "---\nreview_id: stale-review\n"
                "type: contradiction\nstatus: pending\ncreated: 2026-05-30\nupdated: 2026-05-30\n"
                "summary: Stale Review\n"
                "related_pages:\n  - wiki/missing-page.md\n"
                "evidence:\n  - raw/sources/missing-source.md\n"
                "resolution: \"\"\n---\n\n# Stale Review\n"
            )

            result = lint_repo(root)

        issue_codes = [issue.code for issue in result.issues]
        self.assertIn("missing-review-related-page", issue_codes)
        self.assertIn("missing-review-evidence", issue_codes)

    def test_lint_repo_reports_deprecated_page_without_superseded_by(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            page_path = root / "wiki" / "concepts" / "deprecated.md"
            text = VALID_PAGE.replace("status: active", "status: deprecated")
            page_path.write_text(text.replace("# Valid Page", "# Deprecated"))

            result = lint_repo(root)

        self.assertIn("deprecated-page-without-superseded-by", [issue.code for issue in result.issues])

    def test_lint_repo_reports_missing_supersession_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            page_path = root / "wiki" / "concepts" / "superseded.md"
            text = VALID_PAGE.replace(
                "quality:\n",
                "superseded_by:\n  - concepts/missing-target\nquality:\n",
            )
            page_path.write_text(text.replace("# Valid Page", "# Superseded"))

            result = lint_repo(root)

        self.assertIn("missing-supersession-target", [issue.code for issue in result.issues])

    def test_lint_repo_reports_invalid_lifecycle_field_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            page_path = root / "wiki" / "concepts" / "bad-lifecycle.md"
            text = VALID_PAGE.replace("quality:\n", "aliases: bad-alias\nquality:\n")
            page_path.write_text(text.replace("# Valid Page", "# Bad Lifecycle"))

            result = lint_repo(root)

        self.assertIn("invalid-lifecycle-field", [issue.code for issue in result.issues])

    def test_validate_log_reports_invalid_event_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "log.md"
            path.write_text(VALID_PAGE + "\n## [2026-05-26] surprise | Bad Event\n")

            issues = validate_log(parse_page(path))

        self.assertEqual(["invalid-log-event"], [issue.code for issue in issues])

    def test_validate_log_reports_malformed_heading(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "log.md"
            path.write_text(
                VALID_PAGE
                + "\n## [2026-05-26] repair | Good Event\n"
                + "\n## 2026-05-26 bad | Missing Brackets\n"
            )

            issues = validate_log(parse_page(path))

        self.assertIn("invalid-log-heading", [issue.code for issue in issues])

    def test_validate_provenance_reports_missing_signal(self):
        page_text = VALID_PAGE.replace("  provenance: section", "  provenance: section")
        page_text = page_text.replace("Source: Human instruction in `AGENTS.md`.", "No evidence here.")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "unsupported.md"
            path.write_text(page_text)

            issues = validate_provenance(parse_page(path))

        self.assertEqual(["missing-provenance-signal"], [issue.code for issue in issues])

    def test_validate_provenance_requires_claim_evidence_table_for_claim_level(self):
        page_text = _claim_page_text(1, "A claim-level page without a claim evidence table.")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "unsupported-claim.md"
            path.write_text(page_text)

            issues = validate_provenance(parse_page(path), Path(tmpdir))

        self.assertIn("missing-claim-evidence-table", [issue.code for issue in issues])

    def test_validate_provenance_checks_claim_status_evidence_and_source_count(self):
        body = "\n".join(
            [
                "| Claim | Status | Evidence |",
                "|---|---|---|",
                "| Supported claim. | stated | `raw/sources/a.md` |",
                "| Unsupported claim. | guessed |  |",
            ]
        )
        page_text = _claim_page_text(2, body)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "claim-table.md"
            path.write_text(page_text)

            issues = validate_provenance(parse_page(path), Path(tmpdir))

        issue_codes = [issue.code for issue in issues]
        self.assertIn("invalid-claim-status", issue_codes)
        self.assertIn("missing-claim-evidence", issue_codes)
        self.assertIn("source-count-evidence-mismatch", issue_codes)

    def test_validate_provenance_rejects_unregistered_raw_claim_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_source = root / "raw" / "sources" / "unregistered.md"
            raw_source.parent.mkdir(parents=True)
            raw_source.write_text("# Unregistered Source\n")
            page_path = root / "wiki" / "concepts" / "claim.md"
            page_path.parent.mkdir(parents=True)
            page_path.write_text(
                _claim_page_text(
                    1,
                    "\n".join(
                        [
                            "| Claim | Status | Evidence |",
                            "|---|---|---|",
                            "| Claim grounded in raw source. | stated | `raw/sources/unregistered.md` |",
                        ]
                    ),
                )
            )

            issues = validate_provenance(parse_page(page_path), root)

        self.assertIn("claim-evidence-source-unregistered", [issue.code for issue in issues])

    def test_validate_provenance_accepts_registered_raw_claim_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            raw_source = root / "raw" / "sources" / "registered.md"
            raw_source.write_text("# Registered Source\n")
            ingest_source(root, raw_source)
            page_path = root / "wiki" / "concepts" / "claim.md"
            page_path.parent.mkdir(parents=True, exist_ok=True)
            page_path.write_text(
                _claim_page_text(
                    1,
                    "\n".join(
                        [
                            "| Claim | Status | Evidence |",
                            "|---|---|---|",
                            "| Claim grounded in raw source. | stated | `raw/sources/registered.md` |",
                        ]
                    ),
                )
            )

            issues = validate_provenance(parse_page(page_path), root)

        self.assertNotIn("missing-claim-evidence-table", [issue.code for issue in issues])
        self.assertNotIn("source-count-evidence-mismatch", [issue.code for issue in issues])
        self.assertNotIn("claim-evidence-source-unregistered", [issue.code for issue in issues])

    def test_validate_provenance_reports_hash_invalid_claim_evidence_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            raw_source = root / "raw" / "sources" / "drifted.md"
            raw_source.write_text("# Original Source\n")
            ingest_source(root, raw_source)
            raw_source.write_text("# Drifted Source\n")
            page_path = root / "wiki" / "concepts" / "claim.md"
            page_path.parent.mkdir(parents=True, exist_ok=True)
            page_path.write_text(
                _claim_page_text(
                    1,
                    "\n".join(
                        [
                            "| Claim | Status | Evidence |",
                            "|---|---|---|",
                            "| Claim grounded in drifted source. | stated | `raw/sources/drifted.md` |",
                        ]
                    ),
                )
            )

            issues = validate_provenance(parse_page(page_path), root)

        self.assertIn("claim-evidence-source-hash-invalid", [issue.code for issue in issues])

    def test_validate_human_locks_reports_unmatched_markers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "locked.md"
            path.write_text(VALID_PAGE + "\n<!-- human-locked:start -->\n")

            issues = validate_human_locks(parse_page(path))

        self.assertEqual(["unmatched-human-lock"], [issue.code for issue in issues])

    def test_validate_human_locks_reports_misordered_markers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "locked.md"
            path.write_text(
                VALID_PAGE
                + "\n<!-- human-locked:end -->\n"
                + "<!-- human-locked:start -->\n"
            )

            issues = validate_human_locks(parse_page(path))

        self.assertIn("misordered-human-lock", [issue.code for issue in issues])

    def test_find_human_locks_detects_page_lock_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "locked.md"
            path.write_text(VALID_PAGE + "\n<!-- human-locked -->\n")

            state = find_human_locks(parse_page(path))

        self.assertTrue(state.page_locked)
        self.assertEqual([], state.ranges)


class WikiToolCliTest(unittest.TestCase):
    def test_validate_page_cli_accepts_valid_page(self):
        root = Path(__file__).resolve().parents[1]

        result = subprocess.run(
            [sys.executable, "tools/wiki/cli.py", "validate-page", "wiki/overview.md"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("No issues found", result.stdout)

    def test_validate_page_cli_rejects_invalid_page(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "invalid.md"
            path.write_text("# Missing Frontmatter\n")

            result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "validate-page", str(path)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("missing-frontmatter", result.stdout)

    def test_validate_page_cli_checks_claim_evidence_source_hashes(self):
        from tools.wiki import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            raw_source = root / "raw" / "sources" / "validate-page-drift.md"
            raw_source.write_text("# Original Source\n")
            ingest_source(root, raw_source)
            raw_source.write_text("# Drifted Source\n")
            page_path = root / "wiki" / "concepts" / "validate-page-claim.md"
            page_path.write_text(
                _claim_page_text(
                    1,
                    "\n".join(
                        [
                            "| Claim | Status | Evidence |",
                            "|---|---|---|",
                            "| Claim grounded in drifted source. | stated | `raw/sources/validate-page-drift.md` |",
                        ]
                    ),
                )
            )
            original_root = cli.ROOT
            stdout = io.StringIO()

            try:
                cli.ROOT = root
                with contextlib.redirect_stdout(stdout):
                    result = cli.main(["validate-page", str(page_path)])
            finally:
                cli.ROOT = original_root

        self.assertEqual(1, result)
        self.assertIn("claim-evidence-source-hash-invalid", stdout.getvalue())

    def test_validate_page_cli_checks_source_page_hash_drift(self):
        from tools.wiki import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            raw_source = root / "raw" / "sources" / "source-page-drift.md"
            raw_source.write_text("current source\n")
            source_page = root / "wiki" / "sources" / "source-page-drift.md"
            source_page.write_text(
                VALID_PAGE.replace("type: concept", "type: source")
                .replace("source_count: 0", "source_count: 1")
                .replace(
                    "quality:\n",
                    'canonical_source: "raw/sources/source-page-drift.md"\n'
                    'raw_sha256: "not-the-current-hash"\n'
                    "quality:\n",
                )
                .replace("# Valid Page", "# Source Page Drift")
            )
            original_root = cli.ROOT
            stdout = io.StringIO()

            try:
                cli.ROOT = root
                with contextlib.redirect_stdout(stdout):
                    result = cli.main(["validate-page", str(source_page)])
            finally:
                cli.ROOT = original_root

        self.assertEqual(1, result)
        self.assertIn("source-drift", stdout.getvalue())

    def test_lint_cli_accepts_current_vault_and_writes_report(self):
        root = Path(__file__).resolve().parents[1]
        report_path = root / "scratch" / "reports" / "lint-cli-explicit-test.md"
        original_report = report_path.read_text() if report_path.exists() else None

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "lint", "--report", str(report_path)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            report = report_path.read_text()
        finally:
            if original_report is None:
                report_path.unlink(missing_ok=True)
            else:
                report_path.write_text(original_report)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("No issues found", result.stdout)
        self.assertIn("# Wiki Lint Report", report)
        self.assertIn("Issues found: 0", report)

    def test_lint_cli_rejects_report_path_outside_scratch_reports(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "lint.md"

            result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "lint", "--report", str(report_path)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertFalse(report_path.exists())

        self.assertEqual(1, result.returncode)
        self.assertIn("Report path must stay under scratch/reports", result.stderr)

    def test_workflow_query_rejects_report_and_draft_paths_outside_scratch(self):
        from tools.wiki.lib.workflow import QueryCaptureInput, prepare_query_capture_workflow, prepare_query_workflow

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            outside_report = root / "raw" / "sources" / "query-report.md"
            outside_draft = root / "wiki" / "query-capture.json"

            with self.assertRaisesRegex(ValueError, "Report path must stay under scratch/reports"):
                prepare_query_workflow(root, "What changed?", report_path=outside_report)

            with self.assertRaisesRegex(ValueError, "Draft path must stay under scratch/drafts"):
                prepare_query_capture_workflow(
                    root,
                    QueryCaptureInput(
                        question="What changed?",
                        mode="answer-and-capture",
                        answer_summary="Summary.",
                        consulted_pages=[],
                        consulted_sources=[],
                        confidence="medium",
                        contradictions=[],
                        capture_recommendation="Capture.",
                        capture_target="questions/what-changed",
                        capture_title="What Changed",
                    ),
                    draft_path=outside_draft,
                )

    def test_lint_cli_default_report_path_is_dated(self):
        root = Path(__file__).resolve().parents[1]
        report_path = root / "scratch" / "reports" / "lint.md"
        dated_report = root / "scratch" / "reports" / f"{date.today().isoformat()}-lint.md"
        original_report = dated_report.read_text() if dated_report.exists() else None
        if report_path.exists():
            report_path.unlink()

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "lint", "--report"],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertFalse(report_path.exists())
            self.assertTrue(dated_report.exists())
            self.assertIn("## Issues By Severity", dated_report.read_text())
        finally:
            if original_report is None:
                dated_report.unlink(missing_ok=True)
            else:
                dated_report.write_text(original_report)

    def test_health_cli_runs_lint_and_writes_default_report(self):
        root = Path(__file__).resolve().parents[1]
        dated_report = root / "scratch" / "reports" / f"{date.today().isoformat()}-lint.md"
        original_report = dated_report.read_text() if dated_report.exists() else None

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "health"],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("No issues found", result.stdout)
            self.assertIn(f"scratch/reports/{date.today().isoformat()}-lint.md", result.stdout)
            self.assertTrue(dated_report.exists())
            self.assertIn("# Wiki Lint Report", dated_report.read_text())
        finally:
            if original_report is None:
                dated_report.unlink(missing_ok=True)
            else:
                dated_report.write_text(original_report)

    def test_hash_source_cli_prints_sha256(self):
        root = Path(__file__).resolve().parents[1]
        source_path = root / "raw" / "sources" / "hash-source-test.tmp"
        source_path.write_text("stable source\n")
        expected = hashlib.sha256(b"stable source\n").hexdigest()

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "hash-source", str(source_path)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            source_path.unlink(missing_ok=True)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(expected, result.stdout)
        self.assertIn("raw/sources/hash-source-test.tmp", result.stdout)

    def test_lint_reports_source_hash_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw = root / "raw" / "sources"
            source_pages = root / "wiki" / "sources"
            maps = root / "wiki" / "maps"
            raw.mkdir(parents=True)
            source_pages.mkdir(parents=True)
            maps.mkdir(parents=True)
            (raw / "source.md").write_text("changed\n")
            (root / "wiki" / "index.md").write_text(
                VALID_PAGE.replace("type: concept", "type: index").replace(
                    "# Valid Page",
                    "# Index\n\n- [[sources/source]]\n",
                )
            )
            (maps / "map.md").write_text(
                VALID_PAGE.replace("type: concept", "type: map").replace(
                    "# Valid Page",
                    "# Map\n\n- [[sources/source]]\n",
                )
            )
            (source_pages / "source.md").write_text(
                VALID_PAGE.replace("type: concept", "type: source")
                .replace("source_count: 0", "source_count: 1")
                .replace("  provenance: section", "  provenance: claim")
                .replace("# Valid Page", "# Source")
                .replace(
                    "quality:\n",
                    'canonical_source: "raw/sources/source.md"\n'
                    'raw_sha256: "not-the-current-hash"\n'
                    "quality:\n",
                )
            )

            result = lint_repo(root)

        self.assertIn("source-drift", [issue.code for issue in result.issues])

    def test_apply_draft_cli_rejects_write_without_unsafe_flag(self):
        root = Path(__file__).resolve().parents[1]
        target_page = root / "wiki" / "concepts" / "draft-application-test.md"
        draft_path = root / "scratch" / "drafts" / "draft-application-test.json"
        report_path = root / "scratch" / "reports" / "draft-application-test.md"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        draft = {
            "version": 1,
            "operation": "upsert-page",
            "path": "wiki/concepts/draft-application-test.md",
            "frontmatter": {
                "title": "Draft Application Test",
                "type": "concept",
                "status": "active",
                "created": "2026-05-28",
                "updated": "2026-05-28",
                "owner": "agent",
                "summary": "A test page applied from a deterministic draft.",
                "source_count": 0,
                "tags": ["test"],
                "related": ["index"],
                "confidence": "high",
                "quality": {
                    "provenance": "none",
                    "links": "unchecked",
                    "contradictions": "none",
                    "review_required": False,
                },
            },
            "body": "# Draft Application Test\n\nThis page is test content.\n",
            "index": {
                "section": "Concepts",
                "target": "concepts/draft-application-test",
                "summary": "A test page applied from a deterministic draft.",
            },
            "log": {
                "event_type": "repair",
                "title": "Apply Draft Application Test",
                "items": ["Added [[concepts/draft-application-test]]."],
            },
        }
        draft_path.write_text(json.dumps(draft, indent=2))

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "apply-draft",
                    str(draft_path),
                    "--report",
                    str(report_path),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertFalse(target_page.exists())
            self.assertEqual(original_index, (root / "wiki" / "index.md").read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())
            self.assertIn("publish-draft", result.stderr)
        finally:
            target_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)

    def test_publish_draft_cli_applies_valid_draft_then_lints(self):
        root = Path(__file__).resolve().parents[1]
        target_page = root / "wiki" / "concepts" / "publish-draft-test.md"
        draft_path = root / "scratch" / "drafts" / "publish-draft-test.json"
        publish_report = root / "scratch" / "reports" / "publish-draft-test.md"
        lint_report = root / "scratch" / "reports" / f"{date.today().isoformat()}-lint.md"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        original_lint_report = lint_report.read_text() if lint_report.exists() else None
        draft = {
            "version": 1,
            "operation": "upsert-page",
            "path": "wiki/concepts/publish-draft-test.md",
            "frontmatter": {
                "title": "Publish Draft Test",
                "type": "concept",
                "status": "active",
                "created": "2026-05-29",
                "updated": "2026-05-29",
                "owner": "agent",
                "summary": "A test page published through the publish-draft facade.",
                "source_count": 0,
                "tags": ["test"],
                "related": ["index"],
                "confidence": "high",
                "quality": {
                    "provenance": "none",
                    "links": "unchecked",
                    "contradictions": "none",
                    "review_required": False,
                },
            },
            "body": "# Publish Draft Test\n\nPublished through facade.\n",
            "index": {
                "section": "Concepts",
                "target": "concepts/publish-draft-test",
                "summary": "A test page published through the publish-draft facade.",
            },
            "log": {
                "event_type": "repair",
                "title": "Publish Draft Test",
                "items": ["Added [[concepts/publish-draft-test]]."],
            },
        }
        draft_path.write_text(json.dumps(draft, indent=2))

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "publish-draft",
                    str(draft_path),
                    "--report",
                    str(publish_report),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue(target_page.exists())
            self.assertIn("Validated draft", result.stdout)
            self.assertIn("Applied draft", result.stdout)
            self.assertIn("No issues found", result.stdout)
            self.assertIn("Publish Draft Report", publish_report.read_text())
            self.assertIn("# Wiki Lint Report", lint_report.read_text())
        finally:
            target_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)
            publish_report.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)
            if original_lint_report is None:
                lint_report.unlink(missing_ok=True)
            else:
                lint_report.write_text(original_lint_report)

    def test_publish_draft_cli_stops_before_mutation_on_invalid_draft(self):
        root = Path(__file__).resolve().parents[1]
        target_page = root / "wiki" / "concepts" / "publish-invalid-test.md"
        draft_path = root / "scratch" / "drafts" / "publish-invalid-test.json"
        draft = {
            "version": 1,
            "operation": "upsert-page",
            "path": "wiki/concepts/publish-invalid-test.md",
            "frontmatter": {
                "title": "Publish Invalid Test",
                "type": "concept",
            },
            "body": "# Publish Invalid Test\n\nInvalid.\n",
        }
        draft_path.write_text(json.dumps(draft, indent=2))

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "publish-draft", str(draft_path)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertFalse(target_page.exists())
            self.assertIn("Draft failed validation", result.stderr)
            self.assertNotIn("Applied draft", result.stdout)
        finally:
            target_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)

    def test_publish_draft_cli_rejects_invalid_log_event_before_mutation(self):
        root = Path(__file__).resolve().parents[1]
        target_page = root / "wiki" / "concepts" / "publish-invalid-log-event.md"
        draft_path = root / "scratch" / "drafts" / "publish-invalid-log-event.json"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        draft = {
            "version": 1,
            "operation": "upsert-page",
            "path": "wiki/concepts/publish-invalid-log-event.md",
            "frontmatter": {
                "title": "Publish Invalid Log Event",
                "type": "concept",
                "status": "active",
                "created": "2026-05-30",
                "updated": "2026-05-30",
                "owner": "agent",
                "summary": "A valid page with an invalid log event.",
                "source_count": 0,
                "tags": ["test"],
                "related": ["index"],
                "confidence": "high",
                "quality": {
                    "provenance": "none",
                    "links": "unchecked",
                    "contradictions": "none",
                    "review_required": False,
                },
            },
            "body": "# Publish Invalid Log Event\n\nThe page itself is valid.\n",
            "index": {
                "section": "Concepts",
                "target": "concepts/publish-invalid-log-event",
                "summary": "A valid page with an invalid log event.",
            },
            "log": {
                "event_type": "bad-event",
                "title": "Publish Invalid Log Event",
                "items": ["Added [[concepts/publish-invalid-log-event]]."],
            },
        }
        draft_path.write_text(json.dumps(draft, indent=2))

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "publish-draft", str(draft_path)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("invalid-draft-log", result.stderr)
            self.assertNotIn("Applied draft", result.stdout)
            self.assertFalse(target_page.exists())
            self.assertEqual(original_index, (root / "wiki" / "index.md").read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())
        finally:
            target_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)

    def test_apply_draft_dry_run_rejects_source_page_hash_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            raw_source = root / "raw" / "sources" / "draft-source-drift.md"
            raw_source.write_text("# Current Source\n")
            draft_path = root / "scratch" / "drafts" / "draft-source-drift.json"
            draft = {
                "version": 1,
                "operation": "upsert-page",
                "path": "wiki/sources/draft-source-drift.md",
                "frontmatter": {
                    **_frontmatter("Draft Source Drift", "source"),
                    "source_count": 1,
                    "canonical_source": "raw/sources/draft-source-drift.md",
                    "raw_sha256": "not-the-current-hash",
                    "quality": {
                        "provenance": "claim",
                        "links": "unchecked",
                        "contradictions": "none",
                        "review_required": False,
                    },
                },
                "body": "\n".join(
                    [
                        "# Draft Source Drift",
                        "",
                        "Evidence: `raw/sources/draft-source-drift.md`",
                        "",
                        "| Claim | Status | Evidence |",
                        "|---|---|---|",
                        "| Source registered. | stated | `raw/sources/draft-source-drift.md` |",
                        "",
                    ]
                ),
            }
            draft_path.write_text(json.dumps(draft, indent=2))

            with self.assertRaisesRegex(ValueError, "source-drift"):
                apply_draft(root, draft_path, dry_run=True)

    def test_apply_draft_dry_run_rejects_invalid_log_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            draft_path = root / "scratch" / "drafts" / "draft-invalid-log.json"
            draft = {
                "version": 1,
                "operation": "upsert-page",
                "path": "wiki/log.md",
                "frontmatter": _frontmatter("Log", "log"),
                "body": "# Log\n\n## Invalid Log Heading\n\n- Bad.\n",
            }
            draft_path.write_text(json.dumps(draft, indent=2))

            with self.assertRaisesRegex(ValueError, "invalid-log-heading"):
                apply_draft(root, draft_path, dry_run=True)

    def test_apply_draft_dry_run_rejects_human_locked_index_side_effect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            index_path = root / "wiki" / "index.md"
            index_path.write_text(index_path.read_text() + "\n<!-- human-locked -->\n")
            draft_path = root / "scratch" / "drafts" / "draft-index-side-effect-lock.json"
            draft_path.write_text(
                json.dumps(
                    _draft(
                        "wiki/concepts/side-effect-lock.md",
                        "Side Effect Lock",
                        "# Side Effect Lock\n\nSource: Human instruction.\n",
                        "concepts/side-effect-lock",
                    ),
                    indent=2,
                )
            )

            with self.assertRaisesRegex(ValueError, "human-locked"):
                apply_draft(root, draft_path, dry_run=True)

    def test_publish_draft_cli_rejects_claim_level_page_without_claim_evidence_table(self):
        root = Path(__file__).resolve().parents[1]
        target_page = root / "wiki" / "concepts" / "publish-claim-without-table.md"
        draft_path = root / "scratch" / "drafts" / "publish-claim-without-table.json"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        draft = {
            "version": 1,
            "operation": "upsert-page",
            "path": "wiki/concepts/publish-claim-without-table.md",
            "frontmatter": {
                "title": "Publish Claim Without Table",
                "type": "concept",
                "status": "active",
                "created": "2026-05-30",
                "updated": "2026-05-30",
                "owner": "agent",
                "summary": "A claim-level page that should be rejected before durable write.",
                "source_count": 1,
                "tags": ["test"],
                "related": [],
                "confidence": "high",
                "quality": {
                    "provenance": "claim",
                    "links": "unchecked",
                    "contradictions": "none",
                    "review_required": False,
                },
            },
            "body": "# Publish Claim Without Table\n\nEvidence: `raw/sources/missing.md`\n",
            "index": {
                "section": "Concepts",
                "target": "concepts/publish-claim-without-table",
                "summary": "A claim-level page that should be rejected before durable write.",
            },
            "log": {
                "event_type": "repair",
                "title": "Publish Claim Without Table",
                "items": ["Added [[concepts/publish-claim-without-table]]."],
            },
        }
        draft_path.write_text(json.dumps(draft, indent=2))

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "publish-draft", str(draft_path)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("missing-claim-evidence-table", result.stderr)
            self.assertFalse(target_page.exists())
            self.assertEqual(original_index, (root / "wiki" / "index.md").read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())
        finally:
            target_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)

    def test_publish_draft_cli_rolls_back_when_lint_fails_after_apply(self):
        root = Path(__file__).resolve().parents[1]
        target_page = root / "wiki" / "concepts" / "publish-rollback-test.md"
        draft_path = root / "scratch" / "drafts" / "publish-rollback-test.json"
        publish_report = root / "scratch" / "reports" / "publish-rollback-test.md"
        lint_report = root / "scratch" / "reports" / f"{date.today().isoformat()}-lint.md"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        original_lint_report = lint_report.read_text() if lint_report.exists() else None
        draft = {
            "version": 1,
            "operation": "upsert-page",
            "path": "wiki/concepts/publish-rollback-test.md",
            "frontmatter": {
                "title": "Publish Rollback Test",
                "type": "concept",
                "status": "active",
                "created": "2026-05-30",
                "updated": "2026-05-30",
                "owner": "agent",
                "summary": "A test page that should roll back when post-apply lint fails.",
                "source_count": 0,
                "tags": ["test"],
                "related": ["index"],
                "confidence": "high",
                "quality": {
                    "provenance": "none",
                    "links": "unchecked",
                    "contradictions": "none",
                    "review_required": False,
                },
            },
            "body": "# Publish Rollback Test\n\nThe page itself is valid.\n",
            "index": {
                "section": "Concepts",
                "target": "concepts/publish-rollback-test",
                "summary": "A test page that should roll back when post-apply lint fails.",
            },
            "log": {
                "event_type": "repair",
                "title": "Publish Rollback Test",
                "items": ["Added [[missing-log-link-for-rollback]]."],
            },
        }
        draft_path.write_text(json.dumps(draft, indent=2))

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "publish-draft",
                    str(draft_path),
                    "--report",
                    str(publish_report),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("Rolled back draft", result.stdout)
            self.assertFalse(target_page.exists())
            self.assertEqual(original_index, (root / "wiki" / "index.md").read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())
        finally:
            target_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)
            publish_report.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)
            if original_lint_report is None:
                lint_report.unlink(missing_ok=True)
            else:
                lint_report.write_text(original_lint_report)

    def test_publish_batch_cli_applies_multiple_pages_and_writes_manifest_report(self):
        root = Path(__file__).resolve().parents[1]
        first_page = root / "wiki" / "concepts" / "batch-first.md"
        second_page = root / "wiki" / "workflows" / "batch-second.md"
        draft_path = root / "scratch" / "drafts" / "publish-batch-test.json"
        report_path = root / "scratch" / "reports" / "publish-batch-test.md"
        lint_report = root / "scratch" / "reports" / f"{date.today().isoformat()}-lint.md"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        original_lint_report = lint_report.read_text() if lint_report.exists() else None
        batch = {
            "version": 1,
            "operation": "batch-upsert-pages",
            "pages": [
                _batch_page_draft(
                    "wiki/concepts/batch-first.md",
                    "Batch First",
                    "# Batch First\n\nFirst batch page.\n",
                    "concepts/batch-first",
                ),
                {
                    **_batch_page_draft(
                        "wiki/workflows/batch-second.md",
                        "Batch Second",
                        "# Batch Second\n\nSecond batch page.\n",
                    ),
                    "frontmatter": _frontmatter("Batch Second", page_type="workflow"),
                    "index": {
                        "section": "Workflows",
                        "target": "workflows/batch-second",
                        "summary": "Batch Second summary.",
                    },
                },
            ],
            "log": {
                "event_type": "repair",
                "title": "Publish Batch Test",
                "items": [
                    "Added [[concepts/batch-first]].",
                    "Added [[workflows/batch-second]].",
                ],
            },
        }
        draft_path.write_text(json.dumps(batch, indent=2))

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "publish-batch",
                    str(draft_path),
                    "--report",
                    str(report_path),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue(first_page.exists())
            self.assertTrue(second_page.exists())
            self.assertIn("Validated batch draft", result.stdout)
            self.assertIn("Applied batch draft: 2 pages", result.stdout)
            report = report_path.read_text()
            self.assertIn("Publish Batch Report", report)
            self.assertIn("wiki/concepts/batch-first.md", report)
            self.assertIn("wiki/workflows/batch-second.md", report)
            self.assertIn("Added [[concepts/batch-first]].", (root / "wiki" / "log.md").read_text())
        finally:
            first_page.unlink(missing_ok=True)
            second_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)
            if original_lint_report is None:
                lint_report.unlink(missing_ok=True)
            else:
                lint_report.write_text(original_lint_report)

    def test_publish_batch_cli_dry_run_validates_without_writing_pages(self):
        root = Path(__file__).resolve().parents[1]
        target_page = root / "wiki" / "concepts" / "batch-dry-run.md"
        draft_path = root / "scratch" / "drafts" / "publish-batch-dry-run.json"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        batch = {
            "version": 1,
            "operation": "batch-upsert-pages",
            "pages": [
                _batch_page_draft(
                    "wiki/concepts/batch-dry-run.md",
                    "Batch Dry Run",
                    "# Batch Dry Run\n\nDry run only.\n",
                    "concepts/batch-dry-run",
                )
            ],
            "log": {
                "event_type": "repair",
                "title": "Batch Dry Run",
                "items": ["Added [[concepts/batch-dry-run]]."],
            },
        }
        draft_path.write_text(json.dumps(batch, indent=2))

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "publish-batch",
                    str(draft_path),
                    "--dry-run",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Validated batch draft", result.stdout)
            self.assertFalse(target_page.exists())
            self.assertEqual(original_index, (root / "wiki" / "index.md").read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())
        finally:
            target_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)

    def test_publish_batch_cli_stops_before_mutation_when_any_page_is_invalid(self):
        root = Path(__file__).resolve().parents[1]
        valid_page = root / "wiki" / "concepts" / "batch-valid-before-invalid.md"
        invalid_page = root / "wiki" / "concepts" / "batch-invalid.md"
        draft_path = root / "scratch" / "drafts" / "publish-batch-invalid.json"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        invalid = _batch_page_draft(
            "wiki/concepts/batch-invalid.md",
            "Batch Invalid",
            "# Batch Invalid\n\nInvalid page.\n",
            "concepts/batch-invalid",
        )
        invalid["frontmatter"] = {"title": "Batch Invalid", "type": "concept"}
        batch = {
            "version": 1,
            "operation": "batch-upsert-pages",
            "pages": [
                _batch_page_draft(
                    "wiki/concepts/batch-valid-before-invalid.md",
                    "Batch Valid Before Invalid",
                    "# Batch Valid Before Invalid\n\nShould not be written.\n",
                    "concepts/batch-valid-before-invalid",
                ),
                invalid,
            ],
        }
        draft_path.write_text(json.dumps(batch, indent=2))

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "publish-batch", str(draft_path)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("Batch draft failed validation", result.stderr)
            self.assertFalse(valid_page.exists())
            self.assertFalse(invalid_page.exists())
            self.assertEqual(original_index, (root / "wiki" / "index.md").read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())
        finally:
            valid_page.unlink(missing_ok=True)
            invalid_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)

    def test_publish_batch_cli_rolls_back_all_pages_when_post_lint_fails(self):
        root = Path(__file__).resolve().parents[1]
        first_page = root / "wiki" / "concepts" / "batch-rollback-first.md"
        second_page = root / "wiki" / "concepts" / "batch-rollback-second.md"
        draft_path = root / "scratch" / "drafts" / "publish-batch-rollback.json"
        lint_report = root / "scratch" / "reports" / f"{date.today().isoformat()}-lint.md"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        original_lint_report = lint_report.read_text() if lint_report.exists() else None
        batch = {
            "version": 1,
            "operation": "batch-upsert-pages",
            "pages": [
                _batch_page_draft(
                    "wiki/concepts/batch-rollback-first.md",
                    "Batch Rollback First",
                    "# Batch Rollback First\n\nShould roll back.\n",
                    "concepts/batch-rollback-first",
                ),
                _batch_page_draft(
                    "wiki/concepts/batch-rollback-second.md",
                    "Batch Rollback Second",
                    "# Batch Rollback Second\n\nShould roll back.\n",
                    "concepts/batch-rollback-second",
                ),
            ],
            "log": {
                "event_type": "repair",
                "title": "Batch Rollback",
                "items": ["Added [[missing-batch-log-link]]."],
            },
        }
        draft_path.write_text(json.dumps(batch, indent=2))

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "publish-batch", str(draft_path)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("Rolled back batch draft", result.stdout)
            self.assertFalse(first_page.exists())
            self.assertFalse(second_page.exists())
            self.assertEqual(original_index, (root / "wiki" / "index.md").read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())
        finally:
            first_page.unlink(missing_ok=True)
            second_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)
            if original_lint_report is None:
                lint_report.unlink(missing_ok=True)
            else:
                lint_report.write_text(original_lint_report)

    def test_publish_batch_cli_rejects_page_level_logs_before_mutation(self):
        root = Path(__file__).resolve().parents[1]
        target_page = root / "wiki" / "concepts" / "batch-page-log.md"
        draft_path = root / "scratch" / "drafts" / "publish-batch-page-log.json"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        batch = {
            "version": 1,
            "operation": "batch-upsert-pages",
            "pages": [
                _draft(
                    "wiki/concepts/batch-page-log.md",
                    "Batch Page Log",
                    "# Batch Page Log\n\nShould not be written.\n",
                    "concepts/batch-page-log",
                )
            ],
            "log": {
                "event_type": "repair",
                "title": "Batch Page Log",
                "items": ["Added [[concepts/batch-page-log]]."],
            },
        }
        draft_path.write_text(json.dumps(batch, indent=2))

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "publish-batch", str(draft_path)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("invalid-batch-page-log", result.stderr)
            self.assertFalse(target_page.exists())
            self.assertEqual(original_index, (root / "wiki" / "index.md").read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())
        finally:
            target_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)

    def test_init_vault_creates_usable_environment(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "new-wiki"
            init_result = subprocess.run(
                ["./init-vault.sh", str(target)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            lint_result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "lint"],
                cwd=target,
                text=True,
                capture_output=True,
                check=False,
            )
            mcp_result = subprocess.run(
                [sys.executable, "tools/wiki/mcp_server.py"],
                cwd=target,
                input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
                text=True,
                capture_output=True,
                check=False,
            )
            expected_files = [
                target / "AGENTS.md",
                target / ".github" / "workflows" / "release-gate.yml",
                target / "scripts" / "bootstrap.sh",
                target / "scripts" / "release_gate.sh",
                target / "docs" / "usage.md",
                target / "docs" / "architecture.md",
                target / "docs" / "agent" / "DRAFTS.md",
                target / "agents" / "skills" / "wiki-ingest" / "SKILL.md",
                target / "agents" / "skills" / "wiki-update" / "SKILL.md",
                target / "agents" / "skills" / "wiki-query" / "SKILL.md",
                target / "tools" / "stenc" / "validate-stenc-doc.js",
                target / "tools" / "stenc" / "setup-project.js",
                target / "tools" / "stenc" / "check-rendered-pages.js",
                target / "tools" / "wiki" / "templates" / "draft-upsert-page.json",
                target / "tools" / "wiki" / "templates" / "draft-batch-upsert-pages.json",
                target / "tools" / "wiki" / "mcp_server.py",
            ]

            self.assertEqual(0, init_result.returncode, init_result.stdout + init_result.stderr)
            self.assertIn("LLM Wiki Vault initialized", init_result.stdout)
            for path in expected_files:
                self.assertTrue(path.exists(), str(path))
            self.assertTrue((target / "scripts" / "bootstrap.sh").stat().st_mode & 0o111)
            self.assertTrue((target / "scripts" / "release_gate.sh").stat().st_mode & 0o111)
            self.assertEqual(0, lint_result.returncode, lint_result.stdout + lint_result.stderr)
            self.assertIn("No issues found", lint_result.stdout)
            self.assertEqual(0, mcp_result.returncode, mcp_result.stdout + mcp_result.stderr)
            self.assertIn("wiki_publish_draft", mcp_result.stdout)
            self.assertIn("wiki_publish_batch", mcp_result.stdout)

    def test_bootstrap_script_is_valid_bash(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["bash", "-n", "scripts/bootstrap.sh"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_bootstrap_script_initializes_from_archive_url(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            archive_path = tmp / "vault-builder.tar.gz"
            target = tmp / "llm-wiki"
            tar_result = subprocess.run(
                [
                    "tar",
                    "-czf",
                    str(archive_path),
                    "--exclude",
                    ".git",
                    "-C",
                    str(root.parent),
                    root.name,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            env = os.environ.copy()
            env["VAULT_BUILDER_ARCHIVE_URL"] = archive_path.as_uri()
            bootstrap_result = subprocess.run(
                ["bash", "scripts/bootstrap.sh", str(target)],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, tar_result.returncode, tar_result.stdout + tar_result.stderr)
            self.assertEqual(0, bootstrap_result.returncode, bootstrap_result.stdout + bootstrap_result.stderr)
            self.assertTrue((target / "tools" / "wiki" / "cli.py").exists())
            self.assertTrue((target / "scripts" / "bootstrap.sh").exists())
            self.assertIn("Ready:", bootstrap_result.stdout)

    def test_generated_release_gate_ignores_parent_git_worktree(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "parent"
            parent.mkdir()
            target = parent / "new-wiki"
            subprocess.run(["git", "init"], cwd=parent, text=True, capture_output=True, check=True)
            (parent / "outside.txt").write_text("outside trailing whitespace  \n")

            init_result = subprocess.run(
                ["./init-vault.sh", str(target)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            minimal_test = (
                "import unittest\n\n"
                "class MinimalReleaseGateTest(unittest.TestCase):\n"
                "    def test_minimal(self):\n"
                "        self.assertTrue(True)\n"
            )
            (target / "tests" / "test_wiki_quality_baseline.py").write_text(minimal_test)
            (target / "tests" / "test_wiki_tools.py").write_text(minimal_test)
            gate_result = subprocess.run(
                ["scripts/release_gate.sh"],
                cwd=target,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, init_result.returncode, init_result.stdout + init_result.stderr)
            self.assertEqual(0, gate_result.returncode, gate_result.stdout + gate_result.stderr)
            self.assertIn("No Git worktree detected", gate_result.stdout)
            self.assertNotIn("outside.txt", gate_result.stderr)

    def test_ingest_source_cli_registers_raw_source(self):
        root = Path(__file__).resolve().parents[1]
        raw_source = root / "raw" / "sources" / "ingest-source-test.md"
        source_page = root / "wiki" / "sources" / "ingest-source-test.md"
        report_path = root / "scratch" / "reports" / "ingest-source-test.md"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        raw_source.write_text("# Ingest Source Test\n\nSource body.\n")

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "ingest-source",
                    str(raw_source),
                    "--summary",
                    "A test source registered by the ingest command.",
                    "--report",
                    str(report_path),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue(source_page.exists())
            page_text = source_page.read_text()
            self.assertIn("canonical_source: raw/sources/ingest-source-test.md", page_text)
            self.assertIn("raw_sha256:", page_text)
            self.assertIn("[[sources/ingest-source-test]]", (root / "wiki" / "index.md").read_text())
            self.assertIn("Ingest Source Test", (root / "wiki" / "log.md").read_text())
            self.assertIn("Ingested source", report_path.read_text())
        finally:
            raw_source.unlink(missing_ok=True)
            source_page.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)

    def test_ingest_source_is_idempotent_for_same_canonical_source_and_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            raw_source = root / "raw" / "sources" / "repeat.md"
            raw_source.write_text("# Repeat\n\nSame source.\n")

            first = ingest_source(root, raw_source, report_path=root / "scratch" / "reports" / "first.md")
            index_after_first = (root / "wiki" / "index.md").read_text()
            log_after_first = (root / "wiki" / "log.md").read_text()
            second = ingest_source(root, raw_source, report_path=root / "scratch" / "reports" / "second.md")

            self.assertEqual("created", first.outcome)
            self.assertEqual("unchanged", second.outcome)
            self.assertEqual(index_after_first, (root / "wiki" / "index.md").read_text())
            self.assertEqual(log_after_first, (root / "wiki" / "log.md").read_text())

    def test_ingest_source_rejects_report_path_outside_scratch_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            raw_source = root / "raw" / "sources" / "outside-report.md"
            outside_report = Path(tmpdir) / "outside-report.md"
            source_page = root / "wiki" / "sources" / "outside-report.md"
            original_index = (root / "wiki" / "index.md").read_text()
            original_log = (root / "wiki" / "log.md").read_text()
            raw_source.write_text("# Outside Report\n")

            with self.assertRaisesRegex(ValueError, "Report path must stay under scratch/reports"):
                ingest_source(root, raw_source, report_path=outside_report)

            self.assertFalse(outside_report.exists())
            self.assertFalse(source_page.exists())
            self.assertEqual(original_index, (root / "wiki" / "index.md").read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())

    def test_ingest_source_unchanged_rejects_report_path_outside_scratch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            raw_source = root / "raw" / "sources" / "repeat-outside-report.md"
            outside_report = Path(tmpdir) / "repeat-outside-report.md"
            raw_source.write_text("# Repeat Outside Report\n")
            ingest_source(root, raw_source)
            source_page = root / "wiki" / "sources" / "repeat-outside-report.md"
            original_source_page = source_page.read_text()
            original_index = (root / "wiki" / "index.md").read_text()
            original_log = (root / "wiki" / "log.md").read_text()

            with self.assertRaisesRegex(ValueError, "Report path must stay under scratch/reports"):
                ingest_source(root, raw_source, report_path=outside_report)

            self.assertFalse(outside_report.exists())
            self.assertEqual(original_source_page, source_page.read_text())
            self.assertEqual(original_index, (root / "wiki" / "index.md").read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())

    def test_ingest_source_rejects_symlinked_report_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "vault"
            outside = Path(tmpdir) / "outside-reports"
            _minimal_vault(root)
            outside.mkdir()
            shutil.rmtree(root / "scratch" / "reports")
            (root / "scratch" / "reports").symlink_to(outside, target_is_directory=True)
            raw_source = root / "raw" / "sources" / "symlink-report.md"
            report_path = root / "scratch" / "reports" / "escaped.md"
            source_page = root / "wiki" / "sources" / "symlink-report.md"
            original_index = (root / "wiki" / "index.md").read_text()
            original_log = (root / "wiki" / "log.md").read_text()
            raw_source.write_text("# Symlink Report\n")

            with self.assertRaisesRegex(ValueError, "Report path must stay under scratch/reports"):
                ingest_source(root, raw_source, report_path=report_path)

            self.assertFalse((outside / "escaped.md").exists())
            self.assertFalse(source_page.exists())
            self.assertEqual(original_index, (root / "wiki" / "index.md").read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())

    def test_ingest_source_rejects_invalid_report_shape_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            raw_source = root / "raw" / "sources" / "blocked-report.md"
            blocker = root / "scratch" / "reports" / "blocker"
            report_path = blocker / "report.md"
            source_page = root / "wiki" / "sources" / "blocked-report.md"
            original_index = (root / "wiki" / "index.md").read_text()
            original_log = (root / "wiki" / "log.md").read_text()
            raw_source.write_text("# Blocked Report\n")
            blocker.write_text("not a directory\n")

            with self.assertRaisesRegex(ValueError, "Report parent path must be a directory"):
                ingest_source(root, raw_source, report_path=report_path)

            self.assertFalse(source_page.exists())
            self.assertEqual(original_index, (root / "wiki" / "index.md").read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())

    def test_ingest_source_rejects_drift_without_overwriting_source_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            raw_source = root / "raw" / "sources" / "drift.md"
            report_path = root / "scratch" / "reports" / "drift.md"
            raw_source.write_text("original\n")
            ingest_source(root, raw_source)
            source_page = root / "wiki" / "sources" / "drift.md"
            original_page = source_page.read_text()

            raw_source.write_text("changed\n")
            with self.assertRaisesRegex(ValueError, "source-drift"):
                ingest_source(root, raw_source, report_path=report_path)

            self.assertEqual(original_page, source_page.read_text())
            self.assertIn("Outcome: drift", report_path.read_text())

    def test_ingest_source_rejects_slug_collision_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            first_source = root / "raw" / "sources" / "collision.md"
            second_source = root / "raw" / "imports" / "collision.md"
            report_path = root / "scratch" / "reports" / "collision.md"
            first_source.write_text("first\n")
            second_source.write_text("second\n")
            ingest_source(root, first_source)
            source_page = root / "wiki" / "sources" / "collision.md"
            original_page = source_page.read_text()

            with self.assertRaisesRegex(ValueError, "slug-collision"):
                ingest_source(root, second_source, report_path=report_path)

            self.assertEqual(original_page, source_page.read_text())
            self.assertIn("Outcome: collision", report_path.read_text())

    def test_ingest_source_rejects_human_locked_existing_source_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            raw_source = root / "raw" / "sources" / "locked-source.md"
            raw_source.write_text("locked\n")
            ingest_source(root, raw_source)
            source_page = root / "wiki" / "sources" / "locked-source.md"
            source_page.write_text(source_page.read_text() + "\n<!-- human-locked -->\n")
            original_page = source_page.read_text()

            with self.assertRaisesRegex(ValueError, "human-locked"):
                ingest_source(root, raw_source)

            self.assertEqual(original_page, source_page.read_text())

    def test_source_registry_classifies_raw_source_states(self):
        from tools.wiki.lib.source_registry import build_source_registry

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            registered = root / "raw" / "sources" / "registered.md"
            unregistered = root / "raw" / "sources" / "unregistered.md"
            missing = root / "raw" / "sources" / "missing.md"
            registered.write_text("# Registered\n")
            unregistered.write_text("# Unregistered\n")
            missing.write_text("# Missing\n")
            ingest_source(root, registered)
            ingest_source(root, missing)
            missing.unlink()

            entries = build_source_registry(root, checked_at=date(2026, 5, 30))
            by_path = {entry.raw_path: entry for entry in entries}

            self.assertEqual("registered", by_path["raw/sources/registered.md"].ingest_status)
            self.assertEqual("unregistered", by_path["raw/sources/unregistered.md"].ingest_status)
            self.assertEqual("missing-source", by_path["raw/sources/missing.md"].ingest_status)
            self.assertEqual("markdown", by_path["raw/sources/registered.md"].source_kind)
            self.assertEqual("wiki/sources/registered.md", by_path["raw/sources/registered.md"].source_page)
            self.assertEqual("2026-05-30", by_path["raw/sources/unregistered.md"].last_checked)

    def test_bulk_ingest_sources_registers_multiple_raw_files_and_writes_report(self):
        from tools.wiki.lib.source_registry import bulk_ingest_sources

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            first = root / "raw" / "sources" / "bulk-first.md"
            second = root / "raw" / "sources" / "bulk-second.md"
            report_path = root / "scratch" / "reports" / "bulk-ingest.md"
            first.write_text("# Bulk First\n")
            second.write_text("# Bulk Second\n")

            result = bulk_ingest_sources(root, [first, second], report_path=report_path)

            self.assertEqual(["created", "created"], [item.outcome for item in result.items])
            self.assertTrue((root / "wiki" / "sources" / "bulk-first.md").exists())
            self.assertTrue((root / "wiki" / "sources" / "bulk-second.md").exists())
            report = report_path.read_text()
            self.assertIn("Bulk Ingest Report", report)
            self.assertIn("raw/sources/bulk-first.md", report)
            self.assertIn("Created: 2", report)

    def test_bulk_ingest_sources_rolls_back_when_later_source_fails(self):
        from tools.wiki.lib.source_registry import bulk_ingest_sources

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            first = root / "raw" / "sources" / "bulk-rollback-first.md"
            second = root / "raw" / "imports" / "bulk-rollback-first.md"
            report_path = root / "scratch" / "reports" / "bulk-rollback.md"
            original_index = (root / "wiki" / "index.md").read_text()
            original_log = (root / "wiki" / "log.md").read_text()
            first.write_text("# Bulk Rollback First\n")
            second.write_text("# Bulk Rollback Collision\n")

            result = bulk_ingest_sources(root, [first, second], report_path=report_path)

            self.assertFalse(result.ok)
            self.assertEqual(["created", "collision"], [item.outcome for item in result.items])
            self.assertFalse((root / "wiki" / "sources" / "bulk-rollback-first.md").exists())
            self.assertEqual(original_index, (root / "wiki" / "index.md").read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())
            self.assertIn("Collision: 1", report_path.read_text())

    def test_source_registry_cli_writes_report_for_unregistered_raw_file(self):
        root = Path(__file__).resolve().parents[1]
        raw_source = root / "raw" / "sources" / "source-registry-cli-test.md"
        report_path = root / "scratch" / "reports" / "source-registry-cli-test.md"
        original_report = report_path.read_text() if report_path.exists() else None
        raw_source.write_text("# Source Registry CLI Test\n")

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "source-registry",
                    "--report",
                    str(report_path),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Source registry entries", result.stdout)
            report = report_path.read_text()
            self.assertIn("Source Registry Report", report)
            self.assertIn("raw/sources/source-registry-cli-test.md", report)
            self.assertIn("unregistered", report)
        finally:
            raw_source.unlink(missing_ok=True)
            if original_report is None:
                report_path.unlink(missing_ok=True)
            else:
                report_path.write_text(original_report)

    def test_bulk_ingest_cli_registers_multiple_sources(self):
        root = Path(__file__).resolve().parents[1]
        first = root / "raw" / "sources" / "bulk-cli-first.md"
        second = root / "raw" / "sources" / "bulk-cli-second.md"
        first_page = root / "wiki" / "sources" / "bulk-cli-first.md"
        second_page = root / "wiki" / "sources" / "bulk-cli-second.md"
        report_path = root / "scratch" / "reports" / "bulk-cli.md"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        original_report = report_path.read_text() if report_path.exists() else None
        first.write_text("# Bulk CLI First\n")
        second.write_text("# Bulk CLI Second\n")

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "bulk-ingest",
                    str(first),
                    str(second),
                    "--report",
                    str(report_path),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Bulk ingest completed", result.stdout)
            self.assertIn("created: 2", result.stdout)
            self.assertTrue(first_page.exists())
            self.assertTrue(second_page.exists())
            self.assertIn("Created: 2", report_path.read_text())
        finally:
            first.unlink(missing_ok=True)
            second.unlink(missing_ok=True)
            first_page.unlink(missing_ok=True)
            second_page.unlink(missing_ok=True)
            if original_report is None:
                report_path.unlink(missing_ok=True)
            else:
                report_path.write_text(original_report)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)

    def test_bulk_ingest_cli_all_raw_allows_empty_inventory(self):
        from tools.wiki import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            report_path = root / "scratch" / "reports" / "bulk-empty.md"
            original_root = cli.ROOT

            try:
                cli.ROOT = root
                result = cli.main(["bulk-ingest", "--all-raw", "--report", str(report_path)])
            finally:
                cli.ROOT = original_root

            self.assertEqual(0, result)
            report = report_path.read_text()
            self.assertIn("Bulk Ingest Report", report)
            self.assertIn("Total: 0", report)

    def test_review_create_cli_writes_pending_review_item(self):
        root = Path(__file__).resolve().parents[1]
        source_path = root / "raw" / "sources" / "review-create-source.md"
        source_path.write_text("# Review Create Source\n")

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "review",
                    "create",
                    "--type",
                    "contradiction",
                    "--summary",
                    "Review Create Test",
                    "--related",
                    "wiki/overview.md",
                    "--evidence",
                    "raw/sources/review-create-source.md",
                    "--context",
                    "Two grounded claims disagree and need human judgment.",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Review item created", result.stdout)
            review_path = root / result.stdout.split("Review item created: ", 1)[1].strip()
            self.assertTrue(review_path.exists())
            review_text = review_path.read_text()
            self.assertIn("status: pending", review_text)
            self.assertIn("type: contradiction", review_text)
            self.assertIn("wiki/overview.md", review_text)
            self.assertIn("raw/sources/review-create-source.md", review_text)
        finally:
            source_path.unlink(missing_ok=True)
            for path in (root / "scratch" / "review").glob("*review-create-test*.md"):
                path.unlink(missing_ok=True)

    def test_review_resolve_cli_updates_item_and_appends_log(self):
        root = Path(__file__).resolve().parents[1]
        review_path = root / "scratch" / "review" / "2026-05-30-review-resolve-test.md"
        original_log = (root / "wiki" / "log.md").read_text()
        review_path.write_text(
            "\n".join(
                [
                    "---",
                    "review_id: 2026-05-30-review-resolve-test",
                    "type: contested-claim",
                    "status: pending",
                    "created: 2026-05-30",
                    "updated: 2026-05-30",
                    "summary: Review Resolve Test",
                    "related_pages:",
                    "  - wiki/overview.md",
                    "evidence:",
                    "  - AGENTS.md",
                    "resolution: \"\"",
                    "---",
                    "",
                    "# Review Resolve Test",
                    "",
                    "## Context",
                    "",
                    "Pending human judgment.",
                    "",
                    "## Resolution",
                    "",
                    "Pending.",
                    "",
                ]
            )
        )

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "review",
                    "resolve",
                    str(review_path),
                    "--status",
                    "accepted",
                    "--resolution",
                    "Accepted as the current operating interpretation.",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Review item resolved", result.stdout)
            updated_review = review_path.read_text()
            self.assertIn("status: accepted", updated_review)
            self.assertIn("Accepted as the current operating interpretation.", updated_review)
            log_text = (root / "wiki" / "log.md").read_text()
            self.assertIn("manual-note | Review accepted: Review Resolve Test", log_text)
            self.assertIn("[[overview]]", log_text)
            self.assertIn("scratch/review/2026-05-30-review-resolve-test.md", log_text)
        finally:
            review_path.unlink(missing_ok=True)
            (root / "wiki" / "log.md").write_text(original_log)

    def test_review_resolve_cli_rolls_back_when_log_lint_fails(self):
        root = Path(__file__).resolve().parents[1]
        review_path = root / "scratch" / "review" / "2026-05-30-review-resolve-rollback-test.md"
        original_log = (root / "wiki" / "log.md").read_text()
        original_review = (
            "\n".join(
                [
                    "---",
                    "review_id: 2026-05-30-review-resolve-rollback-test",
                    "type: contested-claim",
                    "status: pending",
                    "created: 2026-05-30",
                    "updated: 2026-05-30",
                    "summary: Review Resolve Rollback Test",
                    "related_pages:",
                    "  - wiki/overview.md",
                    "evidence: []",
                    "resolution: \"\"",
                    "---",
                    "",
                    "# Review Resolve Rollback Test",
                    "",
                    "## Resolution",
                    "",
                    "Pending.",
                    "",
                ]
            )
        )
        review_path.write_text(original_review)

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "review",
                    "resolve",
                    str(review_path),
                    "--status",
                    "accepted",
                    "--resolution",
                    "This would create a broken log link to [[missing-review-target]].",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("Rolled back review resolution", result.stdout)
            self.assertEqual(original_review, review_path.read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())
        finally:
            review_path.unlink(missing_ok=True)
            (root / "wiki" / "log.md").write_text(original_log)

    def test_review_list_cli_reports_pending_and_resolved_counts(self):
        root = Path(__file__).resolve().parents[1]
        pending_path = root / "scratch" / "review" / "2026-05-30-review-list-pending.md"
        resolved_path = root / "scratch" / "review" / "2026-05-30-review-list-resolved.md"
        pending_path.write_text(
            "---\nreview_id: pending\n"
            "type: merge\nstatus: pending\ncreated: 2026-05-30\nupdated: 2026-05-30\n"
            "summary: Pending Review\nrelated_pages: []\nevidence: []\nresolution: \"\"\n---\n\n# Pending Review\n"
        )
        resolved_path.write_text(
            "---\nreview_id: resolved\n"
            "type: contradiction\nstatus: resolved\ncreated: 2026-05-30\nupdated: 2026-05-30\n"
            "summary: Resolved Review\nrelated_pages: []\nevidence: []\nresolution: Done\n---\n\n# Resolved Review\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "review", "list"],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Review items: 2", result.stdout)
            self.assertIn("pending: 1", result.stdout)
            self.assertIn("resolved: 1", result.stdout)
            self.assertIn("Pending Review", result.stdout)
            self.assertIn("Resolved Review", result.stdout)
        finally:
            pending_path.unlink(missing_ok=True)
            resolved_path.unlink(missing_ok=True)

    def test_mcp_server_lists_and_calls_review_create_tool(self):
        root = Path(__file__).resolve().parents[1]
        request = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "wiki_review_create",
                        "arguments": {
                            "type": "human-lock",
                            "summary": "MCP Review Create Test",
                            "related": ["wiki/overview.md"],
                            "context": "A human-locked section needs proposed changes.",
                        },
                    },
                }
            )
            + "\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/mcp_server.py"],
                cwd=root,
                input=request,
                text=True,
                capture_output=True,
                check=False,
            )
            responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("wiki_review_create", [tool["name"] for tool in responses[0]["result"]["tools"]])
            self.assertFalse(responses[1]["result"]["isError"])
            self.assertIn("Review item created", responses[1]["result"]["content"][0]["text"])
        finally:
            for path in (root / "scratch" / "review").glob("*mcp-review-create-test*.md"):
                path.unlink(missing_ok=True)

    def test_merge_scan_cli_writes_duplicate_report_and_review_item(self):
        root = Path(__file__).resolve().parents[1]
        first_page = root / "wiki" / "concepts" / "merge-candidate-a.md"
        second_page = root / "wiki" / "concepts" / "merge-candidate-b.md"
        report_path = root / "scratch" / "reports" / "merge-scan-test.md"
        first_page.write_text(render_page(_frontmatter("Merge Candidate"), "# Merge Candidate\n\nFirst candidate.\n"))
        second_page.write_text(render_page(_frontmatter("Merge Candidate"), "# Merge Candidate\n\nSecond candidate.\n"))

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "merge",
                    "scan",
                    "--report",
                    str(report_path),
                    "--create-review",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Merge candidates: 1", result.stdout)
            self.assertIn("Report written", result.stdout)
            self.assertIn("Review item created", result.stdout)
            report = report_path.read_text()
            self.assertIn("Merge Proposal Report", report)
            self.assertIn("wiki/concepts/merge-candidate-a.md", report)
            self.assertIn("wiki/concepts/merge-candidate-b.md", report)
            review_files = list((root / "scratch" / "review").glob("*merge-candidates*.md"))
            self.assertEqual(1, len(review_files))
            self.assertIn("type: merge", review_files[0].read_text())
        finally:
            first_page.unlink(missing_ok=True)
            second_page.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)
            for path in (root / "scratch" / "review").glob("*merge-candidates*.md"):
                path.unlink(missing_ok=True)

    def test_mcp_server_lists_and_calls_merge_scan_tool(self):
        root = Path(__file__).resolve().parents[1]
        report_path = root / "scratch" / "reports" / "mcp-merge-scan-test.md"
        request = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "wiki_merge_scan",
                        "arguments": {"report": "scratch/reports/mcp-merge-scan-test.md"},
                    },
                }
            )
            + "\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/mcp_server.py"],
                cwd=root,
                input=request,
                text=True,
                capture_output=True,
                check=False,
            )
            responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("wiki_merge_scan", [tool["name"] for tool in responses[0]["result"]["tools"]])
            self.assertFalse(responses[1]["result"]["isError"])
            self.assertIn("Merge candidates:", responses[1]["result"]["content"][0]["text"])
            self.assertTrue(report_path.exists())
        finally:
            report_path.unlink(missing_ok=True)

    def test_merge_scan_create_review_is_idempotent_for_same_candidates(self):
        from tools.wiki import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            first_page = root / "wiki" / "concepts" / "dupe-a.md"
            second_page = root / "wiki" / "concepts" / "dupe-b.md"
            first_page.write_text(render_page(_frontmatter("Duplicate Thing"), "# Duplicate A\n\nSource: Human instruction.\n"))
            second_page.write_text(render_page(_frontmatter("Duplicate Thing"), "# Duplicate B\n\nSource: Human instruction.\n"))
            original_root = cli.ROOT

            try:
                cli.ROOT = root
                first = cli.main(["merge", "scan", "--create-review"])
                second = cli.main(["merge", "scan", "--create-review"])
            finally:
                cli.ROOT = original_root

            review_files = list((root / "scratch" / "review").glob("*merge-candidates*.md"))

        self.assertEqual(0, first)
        self.assertEqual(0, second)
        self.assertEqual(1, len(review_files))

    def test_maps_build_cli_generates_navigation_pages_index_and_log(self):
        from tools.wiki import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            (root / "wiki" / "decisions").mkdir()
            (root / "wiki" / "workflows").mkdir()
            (root / "scratch" / "review").mkdir(parents=True)
            source = root / "raw" / "sources" / "navigation-source.md"
            source.write_text("# Navigation Source\n")
            ingest_source(root, source)
            decision = root / "wiki" / "decisions" / "navigation-decision.md"
            decision.write_text(render_page(_frontmatter("Navigation Decision", "decision"), "# Navigation Decision\n\nSource: Human instruction.\n"))
            replacement = root / "wiki" / "concepts" / "replacement-page.md"
            replacement.write_text(render_page(_frontmatter("Replacement Page"), "# Replacement Page\n\nSource: Human instruction.\n"))
            deprecated_fm = _frontmatter("Deprecated Page")
            deprecated_fm["status"] = "deprecated"
            deprecated_fm["superseded_by"] = ["concepts/replacement-page"]
            deprecated = root / "wiki" / "concepts" / "deprecated-page.md"
            deprecated.write_text(render_page(deprecated_fm, "# Deprecated Page\n\nSource: Human instruction.\n"))
            review = root / "scratch" / "review" / "2026-05-30-navigation-review.md"
            review.write_text(
                "---\nreview_id: navigation-review\n"
                "type: merge\nstatus: pending\ncreated: 2026-05-30\nupdated: 2026-05-30\n"
                "summary: Navigation Review\nrelated_pages:\n  - wiki/concepts/deprecated-page.md\n"
                "evidence: []\nresolution: \"\"\n---\n\n# Navigation Review\n"
            )
            report_path = root / "scratch" / "reports" / "maps-build.md"
            original_root = cli.ROOT

            try:
                cli.ROOT = root
                result = cli.main(["maps", "build", "--report", str(report_path)])
            finally:
                cli.ROOT = original_root

            self.assertEqual(0, result)
            for target in [
                "topic-map",
                "source-map",
                "decision-map",
                "review-map",
                "lifecycle-map",
            ]:
                self.assertTrue((root / "wiki" / "maps" / f"{target}.md").exists())

            self.assertIn("[[concepts/deprecated-page]]", (root / "wiki" / "maps" / "topic-map.md").read_text())
            self.assertIn("raw/sources/navigation-source.md", (root / "wiki" / "maps" / "source-map.md").read_text())
            self.assertIn("[[decisions/navigation-decision]]", (root / "wiki" / "maps" / "decision-map.md").read_text())
            self.assertIn("Navigation Review", (root / "wiki" / "maps" / "review-map.md").read_text())
            self.assertIn("[[concepts/deprecated-page]]", (root / "wiki" / "maps" / "lifecycle-map.md").read_text())
            self.assertIn("[[maps/topic-map]]", (root / "wiki" / "index.md").read_text())
            self.assertIn("repair | Rebuild Navigation Maps", (root / "wiki" / "log.md").read_text())
            self.assertIn("Navigation Map Build Report", report_path.read_text())

            check_report = root / "scratch" / "reports" / "maps-check.md"
            try:
                cli.ROOT = root
                check_result = cli.main(["maps", "build", "--check", "--report", str(check_report)])
            finally:
                cli.ROOT = original_root

            self.assertEqual(0, check_result)
            self.assertIn("Mode: check", check_report.read_text())

    def test_maps_build_cli_dry_run_writes_report_without_pages(self):
        from tools.wiki import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            report_path = root / "scratch" / "reports" / "maps-dry-run.md"
            original_root = cli.ROOT

            try:
                cli.ROOT = root
                result = cli.main(["maps", "build", "--dry-run", "--report", str(report_path)])
            finally:
                cli.ROOT = original_root

            self.assertEqual(0, result)
            self.assertFalse((root / "wiki" / "maps" / "topic-map.md").exists())
            self.assertIn("Mode: dry-run", report_path.read_text())

    def test_maps_build_cli_check_fails_when_generated_pages_are_stale(self):
        from tools.wiki import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            report_path = root / "scratch" / "reports" / "maps-check-stale.md"
            original_root = cli.ROOT

            try:
                cli.ROOT = root
                result = cli.main(["maps", "build", "--check", "--report", str(report_path)])
            finally:
                cli.ROOT = original_root

            self.assertEqual(1, result)
            report = report_path.read_text()
            self.assertIn("Mode: check", report)
            self.assertIn("Changed maps: 5", report)

    def test_maps_build_rejects_human_locked_log_side_effect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            log_path = root / "wiki" / "log.md"
            original_log = log_path.read_text() + "\n<!-- human-locked -->\n"
            log_path.write_text(original_log)

            with self.assertRaisesRegex(ValueError, "human-locked"):
                build_navigation_maps(root, build_date=date(2026, 5, 31))

            self.assertEqual(original_log, log_path.read_text())

    def test_maps_build_check_is_stable_when_only_build_date_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)

            build_navigation_maps(root, build_date=date(2026, 5, 30))
            result = build_navigation_maps(root, check=True, build_date=date(2026, 5, 31))

        self.assertEqual([], result.changed_paths)

    def test_maps_build_check_detects_content_changes_after_stable_date_build(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            build_navigation_maps(root, build_date=date(2026, 5, 30))
            new_page = root / "wiki" / "concepts" / "new-map-content.md"
            new_page.write_text(render_page(_frontmatter("New Map Content"), "# New Map Content\n\nSource: Human instruction.\n"))

            result = build_navigation_maps(root, check=True, build_date=date(2026, 5, 31))

        self.assertTrue(result.changed_paths)

    def test_maps_build_logs_index_only_navigation_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            build_navigation_maps(root, build_date=date(2026, 5, 30))
            index_path = root / "wiki" / "index.md"
            index_path.write_text(
                "\n".join(line for line in index_path.read_text().splitlines() if "[[maps/topic-map]]" not in line)
                + "\n"
            )

            result = build_navigation_maps(root, build_date=date(2026, 5, 30))

            self.assertEqual([], result.changed_paths)
            self.assertIn("[[maps/topic-map]]", index_path.read_text())
            self.assertIn("Registered [[maps/topic-map]] in index.", (root / "wiki" / "log.md").read_text())

    def test_topic_map_includes_generated_map_pages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)

            build_navigation_maps(root, build_date=date(2026, 5, 30))
            topic_map = root / "wiki" / "maps" / "topic-map.md"
            text = topic_map.read_text()

        self.assertIn("[[maps/source-map]]", text)
        self.assertIn("[[maps/topic-map]]", text)

    def test_mcp_server_lists_and_calls_maps_build_tool(self):
        root = Path(__file__).resolve().parents[1]
        report_path = root / "scratch" / "reports" / "mcp-maps-build-test.md"
        request = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "wiki_maps_build",
                        "arguments": {"dry_run": True, "report": "scratch/reports/mcp-maps-build-test.md"},
                    },
                }
            )
            + "\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/mcp_server.py"],
                cwd=root,
                input=request,
                text=True,
                capture_output=True,
                check=False,
            )
            responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("wiki_maps_build", [tool["name"] for tool in responses[0]["result"]["tools"]])
            self.assertFalse(responses[1]["result"]["isError"])
            self.assertIn("Navigation maps:", responses[1]["result"]["content"][0]["text"])
            self.assertTrue(report_path.exists())
        finally:
            report_path.unlink(missing_ok=True)

    def test_metrics_cli_writes_sustainable_maintenance_dashboard(self):
        from tools.wiki import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            (root / "wiki" / "claims").mkdir()
            (root / "scratch" / "review").mkdir(parents=True)
            source = root / "raw" / "sources" / "stale-source.md"
            source.write_text("original\n")
            ingest_source(root, source)
            source.write_text("changed\n")
            claim_fm = _frontmatter("Contested Claim", "claim")
            claim_fm["source_count"] = 1
            claim_fm["quality"]["provenance"] = "claim"
            claim_body = "\n".join(
                [
                    "# Contested Claim",
                    "",
                    "| Claim | Status | Evidence |",
                    "|---|---|---|",
                    "| A contested claim needs judgment. | contested | Human instruction |",
                    "",
                ]
            )
            (root / "wiki" / "claims" / "contested-claim.md").write_text(render_page(claim_fm, claim_body))
            replacement = root / "wiki" / "concepts" / "replacement-page.md"
            replacement.write_text(render_page(_frontmatter("Replacement Page"), "# Replacement Page\n\nSource: Human instruction.\n"))
            deprecated_fm = _frontmatter("Deprecated Page")
            deprecated_fm["status"] = "deprecated"
            deprecated_fm["superseded_by"] = ["concepts/replacement-page"]
            deprecated = root / "wiki" / "concepts" / "deprecated-page.md"
            deprecated.write_text(render_page(deprecated_fm, "# Deprecated Page\n\nSource: Human instruction.\n"))
            index_path = root / "wiki" / "index.md"
            index_path.write_text(index_path.read_text() + "\n- [[concepts/deprecated-page]] — Deprecated link.\n")
            review = root / "scratch" / "review" / "2026-05-28-maintenance-review.md"
            review.write_text(
                "---\nreview_id: maintenance-review\n"
                "type: merge\nstatus: pending\ncreated: 2026-05-28\nupdated: 2026-05-28\n"
                "summary: Maintenance Review\nrelated_pages: []\nevidence: []\nresolution: \"\"\n"
                "---\n\n# Maintenance Review\n"
            )
            report_path = root / "scratch" / "reports" / "metrics.md"
            original_root = cli.ROOT

            try:
                cli.ROOT = root
                result = cli.main(["metrics", "--report", str(report_path)])
            finally:
                cli.ROOT = original_root

            self.assertEqual(0, result)
            report = report_path.read_text()
            self.assertIn("Maintenance Metrics Report", report)
            self.assertIn("Pending reviews: 1", report)
            self.assertIn("Contested claim rows: 1", report)
            self.assertIn("Stale sources: 1", report)
            self.assertIn("Deprecated linked pages: 1", report)
            self.assertIn("Pages without claim-level provenance:", report)

    def test_metrics_cli_check_fails_on_sustainability_signals(self):
        from tools.wiki import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            raw_source = root / "raw" / "sources" / "unregistered-signal.md"
            raw_source.write_text("# Unregistered Signal\n")
            original_root = cli.ROOT

            try:
                cli.ROOT = root
                result = cli.main(["metrics", "--check"])
            finally:
                cli.ROOT = original_root

        self.assertEqual(1, result)

    def test_metrics_cli_check_uses_policy_thresholds(self):
        from tools.wiki import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            raw_source = root / "raw" / "sources" / "allowed-unregistered.md"
            policy_path = root / "tools" / "wiki" / "metrics-policy.json"
            raw_source.write_text("# Allowed Unregistered\n")
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text(
                json.dumps(
                    {
                        "max": {
                            "unregistered_sources": 1,
                            "orphan_pages": 1,
                        }
                    },
                    indent=2,
                )
            )
            original_root = cli.ROOT

            try:
                cli.ROOT = root
                result = cli.main(["metrics", "--check", "--policy", str(policy_path)])
            finally:
                cli.ROOT = original_root

        self.assertEqual(0, result)

    def test_metrics_cli_check_fails_for_missing_explicit_policy(self):
        from tools.wiki import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            original_root = cli.ROOT

            try:
                cli.ROOT = root
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    result = cli.main(["metrics", "--check", "--policy", str(root / "missing-policy.json")])
            finally:
                cli.ROOT = original_root

        self.assertEqual(1, result)
        self.assertIn("Metrics policy does not exist", stderr.getvalue())

    def test_mcp_server_lists_and_calls_metrics_tool(self):
        root = Path(__file__).resolve().parents[1]
        report_path = root / "scratch" / "reports" / "mcp-metrics-test.md"
        request = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "wiki_metrics",
                        "arguments": {"report": "scratch/reports/mcp-metrics-test.md"},
                    },
                }
            )
            + "\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/mcp_server.py"],
                cwd=root,
                input=request,
                text=True,
                capture_output=True,
                check=False,
            )
            responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("wiki_metrics", [tool["name"] for tool in responses[0]["result"]["tools"]])
            self.assertFalse(responses[1]["result"]["isError"])
            self.assertIn("Maintenance metrics", responses[1]["result"]["content"][0]["text"])
            self.assertTrue(report_path.exists())
        finally:
            report_path.unlink(missing_ok=True)

    def test_mcp_server_rejects_non_object_json_without_crashing(self):
        root = Path(__file__).resolve().parents[1]

        result = subprocess.run(
            [sys.executable, "tools/wiki/mcp_server.py"],
            cwd=root,
            input="[]\n",
            text=True,
            capture_output=True,
            check=False,
        )
        responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(-32600, responses[0]["error"]["code"])

    def test_repo_local_stenc_setup_renders_pages_from_content_only(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            docs_root = temp_root / "docs" / "stenc"
            shutil.copytree(root / "docs" / "stenc" / "content", docs_root / "content")

            setup = subprocess.run(
                [
                    "node",
                    str(root / "tools" / "stenc" / "setup-project.js"),
                    "--project-root",
                    str(temp_root),
                    "--docs-dir",
                    "docs/stenc",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            check = subprocess.run(
                ["node", str(root / "tools" / "stenc" / "check-rendered-pages.js"), str(docs_root)],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertTrue((docs_root / "index.html").exists())
            self.assertTrue((docs_root / "styles.css").exists())
            self.assertTrue((docs_root / "specs" / "2026-05-30-wiki-workflow-hardening" / "index.html").exists())

        self.assertEqual(0, setup.returncode, setup.stdout + setup.stderr)
        self.assertEqual(0, check.returncode, check.stdout + check.stderr)

    def test_apply_draft_rejects_changed_human_locked_section(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            target_page = root / "wiki" / "concepts" / "locked-section.md"
            draft_path = root / "scratch" / "drafts" / "locked-section.json"
            original_body = "\n".join(
                [
                    "# Locked Section",
                    "",
                    "Editable introduction.",
                    "",
                    "<!-- human-locked:start -->",
                    "Human-owned text.",
                    "<!-- human-locked:end -->",
                    "",
                    "Editable ending.",
                ]
            )
            target_page.write_text(render_page(_frontmatter("Locked Section"), original_body))
            changed_body = original_body.replace("Human-owned text.", "Changed by agent.")
            draft_path.write_text(json.dumps(_draft("wiki/concepts/locked-section.md", "Locked Section", changed_body)))

            with self.assertRaisesRegex(ValueError, "human-locked section"):
                apply_draft(root, draft_path, dry_run=True)

            self.assertIn("Human-owned text.", target_page.read_text())

    def test_apply_draft_allows_changes_outside_preserved_human_locked_section(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            target_page = root / "wiki" / "concepts" / "locked-section.md"
            draft_path = root / "scratch" / "drafts" / "locked-section.json"
            original_body = "\n".join(
                [
                    "# Locked Section",
                    "",
                    "Editable introduction.",
                    "",
                    "<!-- human-locked:start -->",
                    "Human-owned text.",
                    "<!-- human-locked:end -->",
                    "",
                    "Editable ending.",
                ]
            )
            target_page.write_text(render_page(_frontmatter("Locked Section"), original_body))
            changed_body = original_body.replace("Editable ending.", "Updated editable ending.")
            draft_path.write_text(json.dumps(_draft("wiki/concepts/locked-section.md", "Locked Section", changed_body)))

            result = apply_draft(root, draft_path, dry_run=True)

        self.assertEqual("locked-section.md", result.target_path.name)

    def test_publish_draft_cli_rejects_changed_human_locked_section_before_apply(self):
        root = Path(__file__).resolve().parents[1]
        target_page = root / "wiki" / "concepts" / "locked-section-publish-test.md"
        draft_path = root / "scratch" / "drafts" / "locked-section-publish-test.json"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        original_body = "\n".join(
            [
                "# Locked Section Publish Test",
                "",
                "Editable introduction.",
                "",
                "<!-- human-locked:start -->",
                "Human-owned text.",
                "<!-- human-locked:end -->",
                "",
                "Editable ending.",
            ]
        )
        target_page.write_text(render_page(_frontmatter("Locked Section Publish Test"), original_body))
        (root / "wiki" / "index.md").write_text(
            original_index.rstrip()
            + "\n\n## Temporary Test Pages\n\n- [[concepts/locked-section-publish-test]] - Temporary indexed page.\n"
        )
        changed_body = original_body.replace("Human-owned text.", "Changed by agent.")
        draft_path.write_text(
            json.dumps(
                _draft(
                    "wiki/concepts/locked-section-publish-test.md",
                    "Locked Section Publish Test",
                    changed_body,
                    "concepts/locked-section-publish-test",
                ),
                indent=2,
            )
        )

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "publish-draft", str(draft_path)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("human-locked section", result.stderr)
            self.assertIn("Human-owned text.", target_page.read_text())
            self.assertNotIn("Changed by agent.", target_page.read_text())
        finally:
            target_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)

    def test_draft_template_is_valid_apply_draft_input_shape(self):
        root = Path(__file__).resolve().parents[1]
        template_path = root / "tools" / "wiki" / "templates" / "draft-upsert-page.json"

        draft = json.loads(template_path.read_text())

        self.assertEqual(1, draft["version"])
        self.assertEqual("upsert-page", draft["operation"])
        self.assertTrue(str(draft["path"]).startswith("wiki/"))
        self.assertIn("frontmatter", draft)
        self.assertIn("body", draft)
        self.assertIn("index", draft)
        self.assertIn("log", draft)
        self.assertIn("quality", draft["frontmatter"])

    def test_batch_draft_template_is_valid_publish_batch_input_shape(self):
        root = Path(__file__).resolve().parents[1]
        template_path = root / "tools" / "wiki" / "templates" / "draft-batch-upsert-pages.json"

        batch = json.loads(template_path.read_text())

        self.assertEqual(1, batch["version"])
        self.assertEqual("batch-upsert-pages", batch["operation"])
        self.assertIsInstance(batch["pages"], list)
        self.assertGreaterEqual(len(batch["pages"]), 1)
        self.assertIn("log", batch)
        self.assertNotIn("log", batch["pages"][0])
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            _minimal_vault(temp_root)
            draft_path = temp_root / "scratch" / "drafts" / "batch-template.json"
            draft_path.write_text(json.dumps(batch, indent=2))

            result = apply_batch_draft(temp_root, draft_path, dry_run=True)

            self.assertEqual(1, len(result.target_paths))
            self.assertEqual("wiki/concepts/example.md", result.target_paths[0].relative_to(temp_root).as_posix())

    def test_mcp_server_lists_and_calls_hash_source_tool(self):
        root = Path(__file__).resolve().parents[1]
        source_path = root / "raw" / "sources" / "mcp-hash-test.tmp"
        source_path.write_text("mcp source\n")
        request = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "wiki_hash_source",
                        "arguments": {"path": "raw/sources/mcp-hash-test.tmp"},
                    },
                }
            )
            + "\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/mcp_server.py"],
                cwd=root,
                input=request,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            source_path.unlink(missing_ok=True)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(1, responses[0]["id"])
        self.assertIn("wiki_hash_source", [tool["name"] for tool in responses[0]["result"]["tools"]])
        self.assertEqual(2, responses[1]["id"])
        self.assertIn("raw/sources/mcp-hash-test.tmp", responses[1]["result"]["content"][0]["text"])

    def test_mcp_server_lists_and_calls_source_registry_tool(self):
        root = Path(__file__).resolve().parents[1]
        source_path = root / "raw" / "sources" / "mcp-source-registry-test.md"
        report_path = root / "scratch" / "reports" / "mcp-source-registry-test.md"
        original_report = report_path.read_text() if report_path.exists() else None
        source_path.write_text("# MCP Source Registry Test\n")
        request = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "wiki_source_registry",
                        "arguments": {"report": str(report_path)},
                    },
                }
            )
            + "\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/mcp_server.py"],
                cwd=root,
                input=request,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            source_path.unlink(missing_ok=True)
            if original_report is None:
                report_path.unlink(missing_ok=True)
            else:
                report_path.write_text(original_report)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        listed_tools = {tool["name"] for tool in responses[0]["result"]["tools"]}
        self.assertIn("wiki_source_registry", listed_tools)
        self.assertIn("wiki_bulk_ingest", listed_tools)
        self.assertFalse(responses[1]["result"]["isError"])
        self.assertIn("Source registry entries", responses[1]["result"]["content"][0]["text"])

    def test_mcp_server_lists_and_calls_health_tool(self):
        root = Path(__file__).resolve().parents[1]
        lint_report = root / "scratch" / "reports" / f"{date.today().isoformat()}-lint.md"
        original_report = lint_report.read_text() if lint_report.exists() else None
        request = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "wiki_health", "arguments": {}},
                }
            )
            + "\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/mcp_server.py"],
                cwd=root,
                input=request,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            if original_report is None:
                lint_report.unlink(missing_ok=True)
            else:
                lint_report.write_text(original_report)

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertIn("wiki_health", [tool["name"] for tool in responses[0]["result"]["tools"]])
        self.assertFalse(responses[1]["result"]["isError"])
        self.assertIn("No issues found", responses[1]["result"]["content"][0]["text"])

    def test_mcp_apply_draft_validates_without_writing(self):
        root = Path(__file__).resolve().parents[1]
        target_page = root / "wiki" / "concepts" / "mcp-apply-draft-validation-test.md"
        draft_path = root / "scratch" / "drafts" / "mcp-apply-draft-validation-test.json"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        draft = {
            "version": 1,
            "operation": "upsert-page",
            "path": "wiki/concepts/mcp-apply-draft-validation-test.md",
            "frontmatter": {
                "title": "MCP Apply Draft Validation Test",
                "type": "concept",
                "status": "active",
                "created": "2026-05-31",
                "updated": "2026-05-31",
                "owner": "agent",
                "summary": "A test page that MCP apply-draft must validate without writing.",
                "source_count": 0,
                "tags": ["test"],
                "related": ["index"],
                "confidence": "high",
                "quality": {
                    "provenance": "none",
                    "links": "unchecked",
                    "contradictions": "none",
                    "review_required": False,
                },
            },
            "body": "# MCP Apply Draft Validation Test\n\nValidation-only MCP draft.\n",
            "index": {
                "section": "Concepts",
                "target": "concepts/mcp-apply-draft-validation-test",
                "summary": "A test page that MCP apply-draft must validate without writing.",
            },
            "log": {
                "event_type": "repair",
                "title": "MCP Apply Draft Validation Test",
                "items": ["Added [[concepts/mcp-apply-draft-validation-test]]."],
            },
        }
        draft_path.write_text(json.dumps(draft, indent=2))
        request = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "wiki_apply_draft",
                        "arguments": {"path": "scratch/drafts/mcp-apply-draft-validation-test.json"},
                    },
                }
            )
            + "\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/mcp_server.py"],
                cwd=root,
                input=request,
                text=True,
                capture_output=True,
                check=False,
            )
            responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("wiki_apply_draft", [tool["name"] for tool in responses[0]["result"]["tools"]])
            self.assertFalse(responses[1]["result"]["isError"])
            self.assertIn("Validated draft", responses[1]["result"]["content"][0]["text"])
            self.assertFalse(target_page.exists())
            self.assertEqual(original_index, (root / "wiki" / "index.md").read_text())
            self.assertEqual(original_log, (root / "wiki" / "log.md").read_text())
        finally:
            target_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)

    def test_mcp_server_returns_error_for_invalid_tool_arguments(self):
        root = Path(__file__).resolve().parents[1]
        request = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "wiki_workflow_query_capture",
                        "arguments": {
                            "question": "Bad mode?",
                            "mode": "not-a-valid-mode",
                        },
                    },
                }
            )
            + "\n"
        )

        result = subprocess.run(
            [sys.executable, "tools/wiki/mcp_server.py"],
            cwd=root,
            input=request,
            text=True,
            capture_output=True,
            check=False,
        )
        responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(2, responses[1]["id"])
        self.assertTrue(responses[1]["result"]["isError"])
        self.assertIn("invalid choice", responses[1]["result"]["content"][0]["text"])

    def test_mcp_server_lists_and_calls_publish_draft_tool(self):
        root = Path(__file__).resolve().parents[1]
        target_page = root / "wiki" / "concepts" / "mcp-publish-draft-test.md"
        draft_path = root / "scratch" / "drafts" / "mcp-publish-draft-test.json"
        publish_report = root / "scratch" / "reports" / "mcp-publish-draft-test.md"
        lint_report = root / "scratch" / "reports" / f"{date.today().isoformat()}-lint.md"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        original_lint_report = lint_report.read_text() if lint_report.exists() else None
        draft = {
            "version": 1,
            "operation": "upsert-page",
            "path": "wiki/concepts/mcp-publish-draft-test.md",
            "frontmatter": {
                "title": "MCP Publish Draft Test",
                "type": "concept",
                "status": "active",
                "created": "2026-05-29",
                "updated": "2026-05-29",
                "owner": "agent",
                "summary": "A test page published through the MCP facade.",
                "source_count": 0,
                "tags": ["test"],
                "related": ["index"],
                "confidence": "high",
                "quality": {
                    "provenance": "none",
                    "links": "unchecked",
                    "contradictions": "none",
                    "review_required": False,
                },
            },
            "body": "# MCP Publish Draft Test\n\nPublished through MCP facade.\n",
            "index": {
                "section": "Concepts",
                "target": "concepts/mcp-publish-draft-test",
                "summary": "A test page published through the MCP facade.",
            },
            "log": {
                "event_type": "repair",
                "title": "MCP Publish Draft Test",
                "items": ["Added [[concepts/mcp-publish-draft-test]]."],
            },
        }
        draft_path.write_text(json.dumps(draft, indent=2))
        request = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "wiki_publish_draft",
                        "arguments": {
                            "path": "scratch/drafts/mcp-publish-draft-test.json",
                            "report": "scratch/reports/mcp-publish-draft-test.md",
                        },
                    },
                }
            )
            + "\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/mcp_server.py"],
                cwd=root,
                input=request,
                text=True,
                capture_output=True,
                check=False,
            )
            responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("wiki_publish_draft", [tool["name"] for tool in responses[0]["result"]["tools"]])
            self.assertFalse(responses[1]["result"]["isError"])
            self.assertIn("Applied draft", responses[1]["result"]["content"][0]["text"])
            self.assertTrue(target_page.exists())
            self.assertIn("Publish Draft Report", publish_report.read_text())
        finally:
            target_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)
            publish_report.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)
            if original_lint_report is None:
                lint_report.unlink(missing_ok=True)
            else:
                lint_report.write_text(original_lint_report)

    def test_mcp_server_lists_and_calls_publish_batch_tool(self):
        root = Path(__file__).resolve().parents[1]
        target_page = root / "wiki" / "concepts" / "mcp-publish-batch-test.md"
        draft_path = root / "scratch" / "drafts" / "mcp-publish-batch-test.json"
        report_path = root / "scratch" / "reports" / "mcp-publish-batch-test.md"
        lint_report = root / "scratch" / "reports" / f"{date.today().isoformat()}-lint.md"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        original_lint_report = lint_report.read_text() if lint_report.exists() else None
        batch = {
            "version": 1,
            "operation": "batch-upsert-pages",
            "pages": [
                _batch_page_draft(
                    "wiki/concepts/mcp-publish-batch-test.md",
                    "MCP Publish Batch Test",
                    "# MCP Publish Batch Test\n\nPublished through MCP batch facade.\n",
                    "concepts/mcp-publish-batch-test",
                )
            ],
            "log": {
                "event_type": "repair",
                "title": "MCP Publish Batch Test",
                "items": ["Added [[concepts/mcp-publish-batch-test]]."],
            },
        }
        draft_path.write_text(json.dumps(batch, indent=2))
        request = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "wiki_publish_batch",
                        "arguments": {
                            "path": "scratch/drafts/mcp-publish-batch-test.json",
                            "report": "scratch/reports/mcp-publish-batch-test.md",
                        },
                    },
                }
            )
            + "\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/mcp_server.py"],
                cwd=root,
                input=request,
                text=True,
                capture_output=True,
                check=False,
            )
            responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("wiki_publish_batch", [tool["name"] for tool in responses[0]["result"]["tools"]])
            self.assertFalse(responses[1]["result"]["isError"])
            self.assertIn("Applied batch draft", responses[1]["result"]["content"][0]["text"])
            self.assertTrue(target_page.exists())
            self.assertIn("Publish Batch Report", report_path.read_text())
        finally:
            target_page.unlink(missing_ok=True)
            draft_path.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)
            if original_lint_report is None:
                lint_report.unlink(missing_ok=True)
            else:
                lint_report.write_text(original_lint_report)

    def test_workflow_query_prepare_creates_required_report_scaffold(self):
        root = Path(__file__).resolve().parents[1]
        report_path = root / "scratch" / "reports" / f"{date.today().isoformat()}-query-how-does-ingest-work.md"
        original_report = report_path.read_text() if report_path.exists() else None

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "workflow",
                    "query",
                    "--question",
                    "How does ingest work?",
                    "--prepare-report",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Semantic answer synthesis required", result.stdout)
            report = report_path.read_text()
            for heading in [
                "## Question",
                "## Answer Summary",
                "## Pages Consulted",
                "## Sources Consulted",
                "## Confidence",
                "## Contradictions",
                "## Reusable Capture Recommendation",
            ]:
                self.assertIn(heading, report)
        finally:
            if original_report is None:
                report_path.unlink(missing_ok=True)
            else:
                report_path.write_text(original_report)

    def test_workflow_query_prepare_bounds_default_report_filename(self):
        root = Path(__file__).resolve().parents[1]
        long_question = "How does ingest work " + ("with a very long qualifier " * 20)
        created_reports_before = set((root / "scratch" / "reports").glob(f"{date.today().isoformat()}-query-*.md"))

        result = subprocess.run(
            [
                sys.executable,
                "tools/wiki/cli.py",
                "workflow",
                "query",
                "--question",
                long_question,
                "--prepare-report",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        created_reports_after = set((root / "scratch" / "reports").glob(f"{date.today().isoformat()}-query-*.md"))
        new_reports = created_reports_after - created_reports_before

        try:
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(1, len(new_reports), result.stdout + result.stderr)
            report_path = next(iter(new_reports))
            self.assertLessEqual(len(report_path.name), 120)
            self.assertIn("Reusable Capture Recommendation", report_path.read_text())
        finally:
            for path in new_reports:
                path.unlink(missing_ok=True)

    def test_workflow_query_answer_with_report_records_consulted_context(self):
        root = Path(__file__).resolve().parents[1]
        report_path = root / "scratch" / "reports" / "query-answer-with-report-test.md"
        original_report = report_path.read_text() if report_path.exists() else None

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "workflow",
                    "query",
                    "--question",
                    "How does ingest relate to lint?",
                    "--mode",
                    "answer-with-report",
                    "--answer-summary",
                    "Ingest registers sources; lint verifies durable wiki quality.",
                    "--consulted-page",
                    "wiki/overview.md",
                    "--consulted-page",
                    "wiki/workflows/ingest-new-source.md",
                    "--confidence",
                    "high",
                    "--contradiction",
                    "none observed",
                    "--capture-recommendation",
                    "answer-only is enough",
                    "--report",
                    str(report_path),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Query report written", result.stdout)
            report = report_path.read_text()
            self.assertIn("Mode: answer-with-report", report)
            self.assertIn("Ingest registers sources", report)
            self.assertIn("`wiki/overview.md`", report)
            self.assertIn("`wiki/workflows/ingest-new-source.md`", report)
            self.assertIn("- high", report)
            self.assertIn("none observed", report)
            self.assertIn("answer-only is enough", report)
        finally:
            if original_report is None:
                report_path.unlink(missing_ok=True)
            else:
                report_path.write_text(original_report)

    def test_workflow_query_answer_and_capture_creates_publishable_draft_scaffold(self):
        root = Path(__file__).resolve().parents[1]
        draft_path = root / "scratch" / "drafts" / "query-capture-test.json"
        report_path = root / "scratch" / "reports" / "query-capture-test.md"

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "workflow",
                    "query",
                    "--question",
                    "What is the query capture loop?",
                    "--mode",
                    "answer-and-capture",
                    "--answer-summary",
                    "Useful query answers can become durable wiki drafts.",
                    "--consulted-page",
                    "wiki/overview.md",
                    "--confidence",
                    "medium",
                    "--capture-target",
                    "wiki/questions/query-capture-test.md",
                    "--capture-title",
                    "Query Capture Test",
                    "--draft",
                    str(draft_path),
                    "--report",
                    str(report_path),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Query capture draft written", result.stdout)
            self.assertTrue(draft_path.exists())
            self.assertTrue(report_path.exists())
            draft = json.loads(draft_path.read_text())
            self.assertEqual("upsert-page", draft["operation"])
            self.assertEqual("wiki/questions/query-capture-test.md", draft["path"])
            self.assertEqual("question", draft["frontmatter"]["type"])
            self.assertEqual("Query Capture Test", draft["frontmatter"]["title"])
            self.assertIn("Useful query answers", draft["body"])
            self.assertIn("wiki/overview.md", draft["body"])

            dry_run = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "apply-draft", str(draft_path), "--dry-run"],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, dry_run.returncode, dry_run.stdout + dry_run.stderr)
        finally:
            draft_path.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)

    def test_workflow_query_capture_normalizes_wiki_relative_capture_target(self):
        root = Path(__file__).resolve().parents[1]
        draft_path = root / "scratch" / "drafts" / "query-capture-relative-target-test.json"
        report_path = root / "scratch" / "reports" / "query-capture-relative-target-test.md"

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "workflow",
                    "query",
                    "--question",
                    "Can query capture use a wiki-relative target?",
                    "--mode",
                    "answer-and-capture",
                    "--answer-summary",
                    "Query capture targets may be passed as wiki-relative page targets.",
                    "--consulted-page",
                    "wiki/overview.md",
                    "--confidence",
                    "medium",
                    "--capture-target",
                    "questions/query-capture-relative-target-test",
                    "--capture-title",
                    "Query Capture Relative Target Test",
                    "--draft",
                    str(draft_path),
                    "--report",
                    str(report_path),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            draft = json.loads(draft_path.read_text())
            self.assertEqual("wiki/questions/query-capture-relative-target-test.md", draft["path"])
            self.assertEqual("questions/query-capture-relative-target-test", draft["index"]["target"])

            dry_run = subprocess.run(
                [sys.executable, "tools/wiki/cli.py", "apply-draft", str(draft_path), "--dry-run"],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, dry_run.returncode, dry_run.stdout + dry_run.stderr)
        finally:
            draft_path.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)

    def test_workflow_query_capture_rejects_dotdot_target_without_artifacts(self):
        root = Path(__file__).resolve().parents[1]
        draft_path = root / "scratch" / "drafts" / "query-capture-dotdot-target-test.json"
        report_path = root / "scratch" / "reports" / "query-capture-dotdot-target-test.md"

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "workflow",
                    "query",
                    "--question",
                    "Can query capture escape with dotdot?",
                    "--mode",
                    "answer-and-capture",
                    "--answer-summary",
                    "Invalid target.",
                    "--confidence",
                    "medium",
                    "--capture-target",
                    "questions/../dotdot-query-capture",
                    "--capture-title",
                    "Dotdot Query Capture",
                    "--draft",
                    str(draft_path),
                    "--report",
                    str(report_path),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("Capture target must not contain '.' or '..' path segments", result.stderr)
            self.assertFalse(draft_path.exists())
            self.assertFalse(report_path.exists())
        finally:
            draft_path.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)

    def test_workflow_query_capture_rejects_escape_target_without_report_artifact(self):
        root = Path(__file__).resolve().parents[1]
        draft_path = root / "scratch" / "drafts" / "query-capture-escape-target-test.json"
        report_path = root / "scratch" / "reports" / "query-capture-escape-target-test.md"

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "workflow",
                    "query",
                    "--question",
                    "Can query capture escape?",
                    "--mode",
                    "answer-and-capture",
                    "--answer-summary",
                    "Invalid target.",
                    "--confidence",
                    "medium",
                    "--capture-target",
                    "../outside",
                    "--capture-title",
                    "Escape Query Capture",
                    "--draft",
                    str(draft_path),
                    "--report",
                    str(report_path),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertFalse(draft_path.exists())
            self.assertFalse(report_path.exists())
        finally:
            draft_path.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)

    def test_workflow_update_preflight_reports_lock_state_without_mutating_page(self):
        root = Path(__file__).resolve().parents[1]
        target_page = root / "wiki" / "overview.md"
        original_page = target_page.read_text()
        report_path = root / "scratch" / "reports" / "workflow-update-preflight-test.md"
        original_report = report_path.read_text() if report_path.exists() else None

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "workflow",
                    "update",
                    "--target",
                    "wiki/overview.md",
                    "--preflight",
                    "--report",
                    str(report_path),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(original_page, target_page.read_text())
            self.assertIn("Update preflight", result.stdout)
            report = report_path.read_text()
            self.assertIn("Page locked:", report)
            self.assertIn("Section-level locked ranges:", report)
            self.assertIn("Index state:", report)
            self.assertIn("Log state:", report)
            self.assertIn("Semantic update draft required", report)
        finally:
            if original_report is None:
                report_path.unlink(missing_ok=True)
            else:
                report_path.write_text(original_report)

    def test_workflow_update_publish_rejects_draft_target_mismatch(self):
        root = Path(__file__).resolve().parents[1]
        draft_path = root / "scratch" / "drafts" / "workflow-target-mismatch.json"
        draft_path.write_text(
            json.dumps(
                _draft(
                    "wiki/concepts/workflow-target-mismatch.md",
                    "Workflow Target Mismatch",
                    "# Workflow Target Mismatch\n\nDraft body.\n",
                    "concepts/workflow-target-mismatch",
                ),
                indent=2,
            )
        )

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "workflow",
                    "update",
                    "--target",
                    "wiki/overview.md",
                    "--draft",
                    str(draft_path),
                    "--publish",
                    "--report",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("Draft target does not match workflow target", result.stderr)
        finally:
            draft_path.unlink(missing_ok=True)

    def test_workflow_ingest_registers_source_and_marks_semantic_extraction_pending(self):
        root = Path(__file__).resolve().parents[1]
        raw_source = root / "raw" / "sources" / "workflow-ingest-test.md"
        source_page = root / "wiki" / "sources" / "workflow-ingest-test.md"
        report_path = root / "scratch" / "reports" / "workflow-ingest-test.md"
        original_index = (root / "wiki" / "index.md").read_text()
        original_log = (root / "wiki" / "log.md").read_text()
        raw_source.write_text("# Workflow Ingest Test\n\nSource body.\n")

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/wiki/cli.py",
                    "workflow",
                    "ingest",
                    "--source",
                    str(raw_source),
                    "--report",
                    str(report_path),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue(source_page.exists())
            self.assertIn("Semantic extraction pending", result.stdout)
            self.assertIn("Workflow Ingest Report", report_path.read_text())
        finally:
            raw_source.unlink(missing_ok=True)
            source_page.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)
            (root / "wiki" / "index.md").write_text(original_index)
            (root / "wiki" / "log.md").write_text(original_log)

    def test_mcp_server_lists_and_calls_workflow_query_prepare_tool(self):
        root = Path(__file__).resolve().parents[1]
        report_path = root / "scratch" / "reports" / "mcp-workflow-query-prepare-test.md"
        original_report = report_path.read_text() if report_path.exists() else None
        expected_tools = {
            "wiki_workflow_ingest",
            "wiki_workflow_update_preflight",
            "wiki_workflow_update_publish",
            "wiki_workflow_query_prepare",
            "wiki_workflow_query_publish",
        }
        request = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "wiki_workflow_query_prepare",
                        "arguments": {
                            "question": "How does MCP workflow prepare reports?",
                            "report": str(report_path),
                        },
                    },
                }
            )
            + "\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/mcp_server.py"],
                cwd=root,
                input=request,
                text=True,
                capture_output=True,
                check=False,
            )
            responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            listed_tools = {tool["name"] for tool in responses[0]["result"]["tools"]}
            self.assertTrue(expected_tools.issubset(listed_tools))
            self.assertFalse(responses[1]["result"]["isError"])
            self.assertIn("Semantic answer synthesis required", responses[1]["result"]["content"][0]["text"])
            self.assertIn("Reusable Capture Recommendation", report_path.read_text())
        finally:
            if original_report is None:
                report_path.unlink(missing_ok=True)
            else:
                report_path.write_text(original_report)

    def test_mcp_server_calls_workflow_query_capture_mode(self):
        root = Path(__file__).resolve().parents[1]
        draft_path = root / "scratch" / "drafts" / "mcp-query-capture-test.json"
        report_path = root / "scratch" / "reports" / "mcp-query-capture-test.md"
        request = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "wiki_workflow_query_capture",
                        "arguments": {
                            "question": "How should useful query answers be preserved?",
                            "mode": "answer-and-capture",
                            "answer_summary": "Reusable answers should become validated drafts.",
                            "consulted_pages": ["wiki/overview.md"],
                            "confidence": "medium",
                            "capture_target": "wiki/questions/mcp-query-capture-test.md",
                            "capture_title": "MCP Query Capture Test",
                            "draft": "scratch/drafts/mcp-query-capture-test.json",
                            "report": "scratch/reports/mcp-query-capture-test.md",
                        },
                    },
                }
            )
            + "\n"
        )

        try:
            result = subprocess.run(
                [sys.executable, "tools/wiki/mcp_server.py"],
                cwd=root,
                input=request,
                text=True,
                capture_output=True,
                check=False,
            )
            responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("wiki_workflow_query_capture", [tool["name"] for tool in responses[0]["result"]["tools"]])
            self.assertFalse(responses[1]["result"]["isError"])
            self.assertIn("Query capture draft written", responses[1]["result"]["content"][0]["text"])
            self.assertTrue(draft_path.exists())
            self.assertTrue(report_path.exists())
        finally:
            draft_path.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
