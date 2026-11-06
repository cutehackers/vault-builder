"""Page type project at wiki/projects/<project>.md (vault-graph request S9, spec 4.4.7)."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_publish_json import PublishJsonTestCase, _page_draft  # noqa: E402


def _brief_draft(path: str = "wiki/projects/demo.md", project: object = "demo", page_type: str = "project") -> dict:
    draft = copy.deepcopy(_page_draft("unused"))
    draft["path"] = path
    draft["frontmatter"].update(
        {"title": "Demo Brief", "type": page_type, "project": project, "summary": "Goal, structure, and next steps of demo."}
    )
    draft["body"] = "# Demo Brief\n\nGoal: ship v1.\n"
    target = path[len("wiki/") : -len(".md")]
    draft["index"] = {"section": "Overview", "target": target, "summary": "Goal, structure, and next steps of demo."}
    draft["log"]["items"] = [f"Added [[{target}]]."]
    return draft


class ProjectBriefPublishTests(PublishJsonTestCase):
    def test_publish_draft_accepts_a_project_brief(self):
        code, payload, stderr = self.run_json(self.write_draft("brief", _brief_draft()))

        self.assertEqual(0, code, payload)
        self.assertEqual("published", payload["status"])
        self.assertIn("wiki/projects/demo.md", payload["written_paths"])

    def test_publish_draft_rejects_project_type_at_another_path_or_project(self):
        cases = {
            "other-folder": _brief_draft(path="wiki/concepts/demo.md"),
            "other-name": _brief_draft(path="wiki/projects/other.md"),
            "no-project": _brief_draft(project=None),
            "two-projects": _brief_draft(project=["demo", "other"]),
            "other-type-in-projects": _brief_draft(page_type="concept"),
        }
        for name, draft in cases.items():
            with self.subTest(name):
                if draft["frontmatter"]["project"] is None:
                    del draft["frontmatter"]["project"]
                code, payload, _ = self.run_json(self.write_draft(name, draft))
                self.assertEqual(1, code, payload)
                self.assertEqual("rejected", payload["status"])
                self.assertTrue(any("project-page-path" in message for message in payload["messages"]), payload)


class ProjectTypeDocTests(unittest.TestCase):
    def test_type_lists_name_project(self):
        for relative in ("AGENTS.md", "docs/agent/OPERATING-SCHEMA.md", "tools/wiki/templates/frontmatter.yaml"):
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("| inbox | overview | project", text)
        self.assertIn("wiki/projects/<project>.md", (ROOT / "docs/agent/OPERATING-SCHEMA.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
