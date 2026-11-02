"""A patch-page draft that changes only frontmatter (contract 3).

Retiring a memory sends only the new frontmatter keys. The patch is built from the
target file as it is at apply time, so the body is never resent.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.wiki.lib.draft import HumanLockError, apply_draft
from tools.wiki.lib.draft_patch import materialize_patch
from tools.wiki.lib.frontmatter import parse_page, render_page

# The fixture helpers below are copies, not imports from tests/test_wiki_tools.py, so this
# file still runs in a Vault whose test_wiki_tools.py was replaced.
_VALID_PAGE = """---
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
_CLAIM_PAGE_BODY = "\n".join(
    [
        "# Memory Decision",
        "",
        "## Context",
        "",
        "Initial context.",
        "",
        "## Claims",
        "",
        "| Claim | Status | Evidence |",
        "|---|---|---|",
        "| Use SQLite for local state. | stated | Human instruction |",
        "| Keep one writer. | stated | Human instruction |",
        "",
        "## Notes",
        "",
        "First note.",
        "",
    ]
)


def _minimal_vault(root: Path) -> None:
    for relative in ("raw/sources", "raw/imports", "wiki/sources", "wiki/concepts", "scratch/drafts", "scratch/reports"):
        (root / relative).mkdir(parents=True)
    (root / "wiki" / "index.md").write_text(
        _VALID_PAGE.replace("type: concept", "type: index").replace("# Valid Page", "# Index\n\n## Sources\n\n## Concepts")
    )
    (root / "wiki" / "log.md").write_text(
        _VALID_PAGE.replace("type: concept", "type: log").replace(
            "# Valid Page", "# Log\n\n## [2026-05-26] repair | Initial\n\n- Initial log entry."
        )
    )


def _claim_page(root: Path, relative: str = "wiki/decisions/memory-decision.md") -> Path:
    frontmatter = {
        "title": "Memory Decision",
        "type": "decision",
        "status": "active",
        "created": "2026-05-30",
        "updated": "2026-05-30",
        "owner": "agent",
        "summary": "Memory Decision summary.",
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
    }
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_page(frontmatter, _CLAIM_PAGE_BODY))
    return path


def _write_draft(root: Path, draft: dict) -> Path:
    path = root / "scratch" / "drafts" / "draft.json"
    path.write_text(json.dumps(draft))
    return path

TARGET = "wiki/decisions/memory-decision.md"
RETIRE = {"status": "retired", "retired_reason": "replaced by a newer rule", "retired_at": "2026-11-02"}


def _retire_draft(**extra: object) -> dict:
    draft = {"version": 1, "operation": "patch-page", "path": TARGET, "frontmatter": dict(RETIRE)}
    draft.update(extra)
    return draft


class FrontmatterOnlyPatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _minimal_vault(self.root)
        self.target = _claim_page(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_edits_with_frontmatter_changes_only_frontmatter(self):
        before = parse_page(self.target)
        apply_draft(self.root, _write_draft(self.root, _retire_draft(edits=[])))

        after = parse_page(self.target)
        self.assertEqual(before.body, after.body)
        for key, value in RETIRE.items():
            self.assertEqual(value, str(after.frontmatter[key]))
        self.assertEqual(before.frontmatter["title"], after.frontmatter["title"])

    def test_missing_edits_with_frontmatter_applies_through_apply_draft(self):
        before = parse_page(self.target)
        apply_draft(self.root, _write_draft(self.root, _retire_draft()))

        after = parse_page(self.target)
        self.assertEqual(before.body, after.body)
        for key, value in RETIRE.items():
            self.assertEqual(value, str(after.frontmatter[key]))

    def test_non_list_edits_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "edits must be a list"):
            apply_draft(self.root, _write_draft(self.root, _retire_draft(edits="not a list")))

    def test_missing_edits_with_frontmatter_materializes_current_body(self):
        upsert = materialize_patch(self.target, _retire_draft())

        self.assertEqual("upsert-page", upsert["operation"])
        self.assertEqual(parse_page(self.target).body.lstrip("\n"), upsert["body"])
        self.assertEqual("retired", upsert["frontmatter"]["status"])

    def test_frontmatter_patch_uses_file_content_at_apply_time(self):
        draft = _write_draft(self.root, _retire_draft(edits=[]))
        page = parse_page(self.target)
        self.target.write_text(render_page(page.frontmatter, page.body + "\nAdded after the draft was written.\n"))

        apply_draft(self.root, draft)

        after = parse_page(self.target)
        self.assertIn("Added after the draft was written.", after.body)
        self.assertEqual("retired", after.frontmatter["status"])

    def test_patch_without_edits_or_frontmatter_is_rejected(self):
        cases = {
            "empty edits and empty frontmatter": {"edits": [], "frontmatter": {}},
            "empty edits and no frontmatter": {"edits": []},
        }
        for name, fields in cases.items():
            with self.subTest(name=name):
                before = self.target.read_text()
                draft = {"version": 1, "operation": "patch-page", "path": TARGET, **fields}
                with self.assertRaisesRegex(ValueError, "edits or frontmatter"):
                    apply_draft(self.root, _write_draft(self.root, draft))
                self.assertEqual(before, self.target.read_text())

    def test_materialize_rejects_patch_without_edits_or_frontmatter(self):
        draft = {"version": 1, "operation": "patch-page", "path": TARGET}
        with self.assertRaisesRegex(ValueError, "edits or frontmatter"):
            materialize_patch(self.target, draft)

    def test_frontmatter_only_patch_is_rejected_on_human_locked_page(self):
        page = parse_page(self.target)
        self.target.write_text(render_page(page.frontmatter, "<!-- human-locked -->\n" + page.body))
        before = self.target.read_text()

        with self.assertRaises(HumanLockError):
            apply_draft(self.root, _write_draft(self.root, _retire_draft(edits=[])))
        self.assertEqual(before, self.target.read_text())


if __name__ == "__main__":
    unittest.main()
