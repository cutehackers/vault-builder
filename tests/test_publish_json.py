"""publish-draft --json contract (vault-publish-result/v1) and the shared publish_draft function.

Every test runs on a copy of this repository's lint-clean wiki in a temporary folder,
so publishing never touches the working tree.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tools.wiki import cli
from tools.wiki.lib.draft import HumanLockError, apply_draft
from tools.wiki.lib.draft_patch import PatchTargetMissing
from tools.wiki.lib.maps import build_navigation_maps
from tools.wiki.lib.publish import PUBLISH_RESULT_FORMAT, PublishResult, publish_draft

REPO = Path(__file__).resolve().parents[1]
RESULT_FIELDS = {"format", "draft_version", "status", "reason", "written_paths", "messages"}


def _fixture_vault(root: Path) -> None:
    shutil.copytree(REPO / "wiki", root / "wiki")
    shutil.copytree(REPO / "raw", root / "raw")
    shutil.copytree(REPO / "tools" / "wiki", root / "tools" / "wiki", ignore=shutil.ignore_patterns("__pycache__"))
    for folder in ("drafts", "reports", "review"):
        (root / "scratch" / folder).mkdir(parents=True, exist_ok=True)


def _page_draft(slug: str, body: str = "Published through publish_draft.\n", log_item: str | None = None) -> dict:
    return {
        "version": 1,
        "operation": "upsert-page",
        "path": f"wiki/concepts/{slug}.md",
        "frontmatter": {
            "title": "Publish Json Test",
            "type": "concept",
            "status": "active",
            "created": "2026-11-02",
            "updated": "2026-11-02",
            "owner": "agent",
            "summary": "A page published by the publish JSON tests.",
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
        "body": f"# Publish Json Test\n\n{body}",
        "index": {
            "section": "Concepts",
            "target": f"concepts/{slug}",
            "summary": "A page published by the publish JSON tests.",
        },
        "log": {
            "event_type": "repair",
            "title": "Publish Json Test",
            "items": [log_item or f"Added [[concepts/{slug}]]."],
        },
    }


class PublishJsonTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _fixture_vault(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_draft(self, name: str, draft: dict) -> Path:
        path = self.root / "scratch" / "drafts" / f"{name}.json"
        path.write_text(json.dumps(draft, indent=2))
        return path

    def run_json(self, draft_path: Path, *extra: str) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with unittest.mock.patch.object(cli, "ROOT", self.root):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(["publish-draft", str(draft_path), "--json", *extra])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(RESULT_FIELDS, set(payload))
        self.assertEqual(PUBLISH_RESULT_FORMAT, payload["format"])
        self.assertEqual("vault-publish-result/v1", payload["format"])
        return code, payload, stderr.getvalue()

    def snapshot_side_files(self) -> tuple[str, str]:
        return (self.root / "wiki" / "index.md").read_text(), (self.root / "wiki" / "log.md").read_text()

    def assert_rejected(self, code: int, payload: dict, reason: str) -> None:
        self.assertEqual(1, code, payload)
        self.assertEqual("rejected", payload["status"])
        self.assertEqual(reason, payload["reason"])
        self.assertEqual([], payload["written_paths"])
        self.assertTrue(payload["messages"], payload)
        self.assertTrue(all(isinstance(message, str) for message in payload["messages"]))


class PublishDraftJsonReasonTests(PublishJsonTestCase):
    def test_published_reports_null_reason_written_paths_and_rebuilds_maps(self):
        draft_path = self.write_draft("published", _page_draft("publish-json-ok"))

        code, payload, stderr = self.run_json(draft_path)

        self.assertEqual(0, code, payload)
        self.assertEqual("published", payload["status"])
        self.assertIsNone(payload["reason"])
        self.assertEqual(1, payload["draft_version"])
        self.assertIn("wiki/concepts/publish-json-ok.md", payload["written_paths"])
        self.assertIn("wiki/index.md", payload["written_paths"])
        self.assertIn("wiki/log.md", payload["written_paths"])
        self.assertTrue(all(not Path(path).is_absolute() for path in payload["written_paths"]))
        self.assertTrue((self.root / "wiki" / "concepts" / "publish-json-ok.md").exists())
        self.assertIn("Validated draft", stderr)
        self.assertIn("Applied draft", stderr)
        self.assertIn("Rebuilt navigation maps", stderr)
        self.assertEqual([], build_navigation_maps(self.root, dry_run=True).changed_paths)

    def test_lint_dirty_before_rejects_without_touching_the_wiki(self):
        (self.root / "wiki" / "concepts" / "broken.md").write_text("---\ntitle: \"Broken\"\n---\n# Broken\n")
        index_before, log_before = self.snapshot_side_files()
        draft_path = self.write_draft("dirty", _page_draft("publish-json-dirty"))

        code, payload, stderr = self.run_json(draft_path)

        self.assert_rejected(code, payload, "lint-dirty-before")
        self.assertEqual(1, payload["draft_version"])
        self.assertTrue(any("broken.md" in message for message in payload["messages"]))
        self.assertIn("Publish aborted", stderr)
        self.assertFalse((self.root / "wiki" / "concepts" / "publish-json-dirty.md").exists())
        self.assertEqual((index_before, log_before), self.snapshot_side_files())

    def test_draft_invalid_rejects_with_validation_text(self):
        draft = _page_draft("publish-json-invalid")
        draft["frontmatter"] = {"title": "Invalid", "type": "concept"}
        draft_path = self.write_draft("invalid", draft)

        code, payload, stderr = self.run_json(draft_path)

        self.assert_rejected(code, payload, "draft-invalid")
        self.assertTrue(any("Draft failed validation" in message for message in payload["messages"]))
        self.assertIn("Draft failed validation", stderr)
        self.assertFalse((self.root / "wiki" / "concepts" / "publish-json-invalid.md").exists())

    def test_human_locked_page_is_rejected_with_its_own_reason(self):
        first = publish_draft(self.root, self.write_draft("lock-first", _page_draft("publish-json-locked")))
        self.assertEqual("published", first.status, first.messages)
        target = self.root / "wiki" / "concepts" / "publish-json-locked.md"
        target.write_text(target.read_text() + "\n<!-- human-locked -->\n")
        locked_text = target.read_text()
        update = _page_draft("publish-json-locked", body="Changed by an agent.\n")
        update["index"] = None
        update["log"] = None

        code, payload, _stderr = self.run_json(self.write_draft("lock-second", update))

        self.assert_rejected(code, payload, "human-locked")
        self.assertTrue(any("human-locked" in message for message in payload["messages"]))
        self.assertEqual(locked_text, target.read_text())

    def test_patch_on_missing_page_is_page_missing(self):
        patch = {
            "version": 1,
            "operation": "patch-page",
            "path": "wiki/concepts/publish-json-missing.md",
            "edits": [{"op": "append-to-section", "heading": "## Notes", "text": "x"}],
        }

        code, payload, _stderr = self.run_json(self.write_draft("missing", patch))

        self.assert_rejected(code, payload, "page-missing")
        self.assertFalse((self.root / "wiki" / "concepts" / "publish-json-missing.md").exists())

    def test_lint_failed_after_rolls_back_every_written_file(self):
        index_before, log_before = self.snapshot_side_files()
        draft = _page_draft("publish-json-rollback", log_item="Added [[missing-log-link-for-rollback]].")

        code, payload, stderr = self.run_json(self.write_draft("rollback", draft))

        self.assert_rejected(code, payload, "lint-failed-after")
        self.assertIn("Rolled back draft", stderr)
        self.assertFalse((self.root / "wiki" / "concepts" / "publish-json-rollback.md").exists())
        self.assertEqual((index_before, log_before), self.snapshot_side_files())

    def test_unreadable_draft_is_io_error(self):
        code, payload, _stderr = self.run_json(self.root / "scratch" / "drafts" / "does-not-exist.json")

        self.assert_rejected(code, payload, "io-error")
        self.assertIsNone(payload["draft_version"])


class PublishDraftJsonOutputTests(PublishJsonTestCase):
    def test_json_stdout_is_one_object_from_the_vault_copy_of_the_cli(self):
        draft_path = self.write_draft("subprocess", _page_draft("publish-json-subprocess"))

        result = subprocess.run(
            [sys.executable, "-I", "-B", "tools/wiki/cli.py", "publish-draft", str(draft_path), "--json"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("published", payload["status"])
        self.assertIn("Applied draft", result.stderr)
        self.assertNotIn("Applied draft", result.stdout)

    def test_text_output_without_json_keeps_streams_and_exit_codes(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        draft_path = self.write_draft("text", _page_draft("publish-json-text"))
        with unittest.mock.patch.object(cli, "ROOT", self.root):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(["publish-draft", str(draft_path)])

        self.assertEqual(0, code)
        self.assertIn("Validated draft: wiki/concepts/publish-json-text.md", stdout.getvalue())
        self.assertIn("Applied draft: wiki/concepts/publish-json-text.md", stdout.getvalue())
        self.assertIn("No issues found.", stdout.getvalue())
        self.assertIn("Rebuilt navigation maps", stdout.getvalue())
        with self.assertRaises(json.JSONDecodeError):
            json.loads(stdout.getvalue())


class PublishDraftFunctionTests(PublishJsonTestCase):
    def test_publish_draft_returns_result_with_report(self):
        report = self.root / "scratch" / "reports" / "publish-json-report.md"
        result = publish_draft(self.root, self.write_draft("fn", _page_draft("publish-json-fn")), report_path=report)

        self.assertIsInstance(result, PublishResult)
        self.assertEqual("published", result.status)
        self.assertIn("scratch/reports/publish-json-report.md", result.written_paths)
        self.assertIn("Publish Draft Report", report.read_text())
        self.assertEqual(0, result.exit_code)

    def test_report_path_outside_reports_rolls_back(self):
        index_before, log_before = self.snapshot_side_files()
        result = publish_draft(
            self.root,
            self.write_draft("bad-report", _page_draft("publish-json-bad-report")),
            report_path=self.root / "wiki" / "not-a-report.md",
        )

        self.assertEqual("rejected", result.status)
        self.assertEqual("io-error", result.reason)
        self.assertEqual(1, result.exit_code)
        self.assertFalse((self.root / "wiki" / "concepts" / "publish-json-bad-report.md").exists())
        self.assertEqual((index_before, log_before), self.snapshot_side_files())


class RejectionErrorTypeTests(PublishJsonTestCase):
    def test_human_lock_and_missing_patch_target_are_value_error_subclasses(self):
        self.assertTrue(issubclass(HumanLockError, ValueError))
        self.assertTrue(issubclass(PatchTargetMissing, ValueError))
        patch = {
            "version": 1,
            "operation": "patch-page",
            "path": "wiki/concepts/nowhere.md",
            "edits": [{"op": "append-to-section", "heading": "## Notes", "text": "x"}],
        }
        with self.assertRaises(PatchTargetMissing):
            apply_draft(self.root, self.write_draft("nowhere", patch), dry_run=True)


if __name__ == "__main__":
    unittest.main()
