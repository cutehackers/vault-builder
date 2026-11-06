"""Restoring a retired page with a frontmatter-only patch-page draft (vault-graph spec 4.4.13, risk D12, request S12).

vault-graph restore_memory sends status active, restored_at, and restored_reason, and keeps
retired_reason, retired_at, and superseded_by as history. Validation must accept that page.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_publish_json import PublishJsonTestCase, _page_draft  # noqa: E402
from tools.wiki.lib.frontmatter import Page, parse_page  # noqa: E402
from tools.wiki.lib.memory_fields import validate_memory_fields  # noqa: E402


def _patch(path: str, frontmatter: dict) -> dict:
    return {
        "version": 1,
        "operation": "patch-page",
        "path": path,
        "frontmatter": frontmatter,
        "log": {"event_type": "repair", "title": "Restore test", "items": ["Changed a page status."]},
    }


class RestoreRetiredPageTests(PublishJsonTestCase):
    PATH = "wiki/concepts/restore-me.md"

    def setUp(self) -> None:
        super().setUp()
        code, payload, _ = self.run_json(self.write_draft("create", _page_draft("restore-me")))
        self.assertEqual(0, code, payload)

    def publish_patch(self, name: str, frontmatter: dict) -> tuple[int, dict]:
        code, payload, _ = self.run_json(self.write_draft(name, _patch(self.PATH, frontmatter)))
        return code, payload

    def body(self) -> str:
        return (self.root / self.PATH).read_text(encoding="utf-8").split("\n---\n", 1)[1]

    def test_restore_after_retire_is_published_and_keeps_history_and_body(self):
        code, payload = self.publish_patch(
            "retire", {"status": "retired", "retired_reason": "Wrong.", "retired_at": "2026-11-01"}
        )
        self.assertEqual(0, code, payload)
        body = self.body()

        code, payload = self.publish_patch(
            "restore", {"status": "active", "restored_at": "2026-11-06", "restored_reason": "True again."}
        )

        self.assertEqual(0, code, payload)
        frontmatter = parse_page(self.root / self.PATH).frontmatter
        self.assertEqual("active", frontmatter["status"])
        self.assertEqual("Wrong.", frontmatter["retired_reason"])
        self.assertEqual("2026-11-01", str(frontmatter["retired_at"]))
        self.assertEqual("2026-11-06", str(frontmatter["restored_at"]))
        self.assertEqual("True again.", frontmatter["restored_reason"])
        self.assertEqual(body, self.body())

    def test_restore_after_deprecate_keeps_superseded_by(self):
        code, payload, _ = self.run_json(self.write_draft("new", _page_draft("replacement")))
        self.assertEqual(0, code, payload)
        code, payload = self.publish_patch(
            "deprecate", {"status": "deprecated", "superseded_by": ["concepts/replacement"]}
        )
        self.assertEqual(0, code, payload)

        code, payload = self.publish_patch(
            "restore", {"status": "active", "restored_at": "2026-11-06", "restored_reason": "Still holds."}
        )

        self.assertEqual(0, code, payload)
        self.assertIn("superseded_by:", (self.root / self.PATH).read_text(encoding="utf-8"))


class RestoredFieldRuleTests(unittest.TestCase):
    def issues(self, **frontmatter: object) -> set[str]:
        page = Page(path=Path("wiki/concepts/x.md"), frontmatter=frontmatter, body="")
        return {issue.code for issue in validate_memory_fields(page)}

    def test_retired_fields_on_an_active_page_need_restored_fields(self):
        self.assertIn("retired-field-without-retired-status",
                      self.issues(status="active", retired_reason="x", retired_at="2026-11-01"))
        self.assertEqual(set(), self.issues(status="active", retired_reason="x", retired_at="2026-11-01",
                                            restored_at="2026-11-06", restored_reason="back"))

    def test_restored_fields_come_together_with_a_date_and_a_reason(self):
        self.assertIn("restored-fields-incomplete", self.issues(status="active", restored_at="2026-11-06"))
        self.assertIn("restored-fields-incomplete", self.issues(status="active", restored_reason="back"))
        self.assertIn("invalid-restored-at", self.issues(status="active", restored_at="soon", restored_reason="back"))
        self.assertIn("restored-fields-incomplete", self.issues(status="active", restored_at="2026-11-06",
                                                                 restored_reason=" "))


if __name__ == "__main__":
    unittest.main()
