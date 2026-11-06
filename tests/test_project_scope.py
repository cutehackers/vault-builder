"""Pages without `project` for the fallback project summary (vault-graph request S4, spec 4.4.2).

maps build keeps writing one summary per project id from page frontmatter, so its output does not
depend on the vault map. `maps no-project-summary` prints the same sections for pages and open inbox
items that have no `project`. recall.sh decides with the vault map whether a project sees them.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_wiki_tools import (  # noqa: E402
    _project_fixture_vault,
    _run_wiki_cli,
    _write_inbox_items,
    _write_project_page,
)

INBOX = """
## [2026-05-29] Alpha work

Status: deferred
Project: alpha
Source: Human instruction.
Note: Open work for alpha.
Next action: Build it.

## [2026-05-28] Shared work

Status: unprocessed
Source: Human instruction.
Note: Open work without a project.
Next action: Look at it.
"""


def _page_without_project(root: Path, relative: str, title: str, page_type: str, tags: list[str], **extra) -> Path:
    path = _write_project_page(root, relative, title, page_type, "placeholder", tags, **extra)
    text = path.read_text()
    path.write_text("".join(line for line in text.splitlines(keepends=True) if not line.startswith("project:")))
    return path


class NoProjectSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.root = Path(tmpdir.name)
        _project_fixture_vault(self.root)
        _write_project_page(self.root, "wiki/decisions/alpha-storage.md", "Alpha Storage", "decision", "alpha", [])
        _page_without_project(self.root, "wiki/decisions/shared-format.md", "Shared Format", "decision", [],
                              summary="Use one format everywhere.")
        _page_without_project(self.root, "wiki/concepts/shared-rule.md", "Shared Rule", "concept", ["rule"],
                              summary="Always run the gate.")
        _page_without_project(self.root, "wiki/decisions/old-shared.md", "Old Shared", "decision", [],
                              status="retired", summary="Retired decision.")
        _write_inbox_items(self.root, INBOX)

    def test_prints_only_items_without_project(self):
        code, output = _run_wiki_cli(self.root, "maps", "no-project-summary")

        self.assertEqual(0, code, output)
        self.assertIn("## Open Work", output)
        self.assertIn("## Current Decisions", output)
        self.assertIn("## Corrections and Rules", output)
        self.assertIn("Shared work", output)
        self.assertNotIn("Alpha work", output)
        self.assertIn("Use one format everywhere. (wiki/decisions/shared-format.md)", output)
        self.assertIn("Always run the gate. (wiki/concepts/shared-rule.md)", output)
        self.assertNotIn("alpha-storage", output)
        self.assertNotIn("Retired decision.", output)

    def test_is_read_only_and_maps_build_output_does_not_change(self):
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        _run_wiki_cli(self.root, "maps", "no-project-summary")
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

        _run_wiki_cli(self.root, "maps", "build")
        summaries = sorted(path.name for path in (self.root / "wiki" / "maps" / "projects").glob("*.md"))
        self.assertEqual(["alpha.md"], summaries)
        alpha = (self.root / "wiki" / "maps" / "projects" / "alpha.md").read_text(encoding="utf-8")
        self.assertNotIn("shared-format", alpha)


class MemoryCleanupScopeDocTests(unittest.TestCase):
    def test_memory_cleanup_skill_states_the_scope_rule(self):
        text = (ROOT / ".agents" / "skills" / "memory-cleanup" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("sharable", text)
        self.assertIn("linked to this project only", text)


if __name__ == "__main__":
    unittest.main()
