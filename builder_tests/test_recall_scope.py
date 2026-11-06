"""recall.sh follows the project scope rule of vault-graph spec 4.4.2 (request S4).

A page whose `project` names this project comes from the summary page, as before. Pages without
`project` come from the vault's read-only `maps no-project-summary` command, and only when the vault
is linked to this project only, or is linked to two or more projects with sharable true.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from builder_tests.test_vault_migration import (
    ROOT,
    _init_git_repo,
    _materialize_recall_script,
    _small_sections,
    _write_summary_page,
)

SHARED_DECISION = "Use one format everywhere"


def _vault_with_tools(path: Path) -> Path:
    (path / "wiki" / "decisions").mkdir(parents=True)
    # A copy, not a link: cli.py finds its vault from its own resolved path.
    shutil.copytree(ROOT / "tools" / "wiki", path / "tools" / "wiki", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("__init__.py",):
        if (ROOT / "tools" / name).is_file():
            shutil.copy2(ROOT / "tools" / name, path / "tools" / name)
    (path / "wiki" / "decisions" / "shared-format.md").write_text(
        "---\n"
        'title: "Shared Format"\n'
        "type: decision\n"
        "status: active\n"
        "updated: 2026-11-01\n"
        f'summary: "{SHARED_DECISION}."\n'
        "---\n\n# Shared Format\n",
        encoding="utf-8",
    )
    return path


class _RecallCase(unittest.TestCase):
    def setUp(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.tmp = Path(tmpdir.name).resolve()
        self.home = self.tmp / "home"
        self.config = self.home / ".claude"
        self.script = _materialize_recall_script(self.config / "skills" / "project-memory" / "scripts" / "recall.sh")
        self.repo = _init_git_repo(self.tmp / "work" / "sample-app")
        self.other = _init_git_repo(self.tmp / "work" / "other-app")
        self.vault = _vault_with_tools(self.tmp / "vaults" / "personal")
        _write_summary_page(self.vault, "sample-app", _small_sections())

    def write_map(self, projects: dict[str, list[str]], sharable: object = None) -> None:
        vault: dict[str, object] = {"path": str(self.vault), "role": "personal"}
        if sharable is not None:
            vault["sharable"] = sharable
        roots = {"sample-app": self.repo, "other-app": self.other}
        data = {
            "version": 2,
            "vaults": {"personal": vault},
            "projects": {name: {"root": str(roots[name]), "vaults": names} for name, names in projects.items()},
        }
        path = self.config / "vault-builder" / "vault-map.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def recall(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", ""), "GIT_CEILING_DIRECTORIES": str(self.tmp)}
        return subprocess.run([str(self.script), *arguments], cwd=self.repo, text=True, capture_output=True,
                              check=False, env=env, timeout=60)


class RecallScopeTests(_RecallCase):
    def test_vault_linked_to_this_project_only_shows_pages_without_project(self):
        self.write_map({"sample-app": ["personal"]})
        result = self.recall()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(SHARED_DECISION, result.stdout)
        self.assertIn(_small_sections()[1][0], result.stdout)
        self.assertEqual(1, result.stdout.count("## Current Decisions"))

    def test_shared_vault_without_sharable_hides_pages_without_project(self):
        for sharable in (None, False, "yes"):
            with self.subTest(sharable=sharable):
                self.write_map({"sample-app": ["personal"], "other-app": ["personal"]}, sharable)
                result = self.recall()
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertNotIn(SHARED_DECISION, result.stdout)
                self.assertIn(_small_sections()[1][0], result.stdout)

    def test_shared_vault_with_sharable_true_shows_pages_without_project(self):
        self.write_map({"sample-app": ["personal"], "other-app": ["personal"]}, True)
        result = self.recall("decisions")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(SHARED_DECISION, result.stdout)
        self.assertIn(_small_sections()[1][0], result.stdout)

    def test_pages_without_project_show_even_without_a_summary_page(self):
        (self.vault / "wiki" / "maps" / "projects" / "sample-app.md").unlink()
        self.write_map({"sample-app": ["personal"]})
        result = self.recall("decisions")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(SHARED_DECISION, result.stdout)
        self.assertIn("# vault personal: pages without project", result.stdout)


class RecallBriefTests(_RecallCase):
    """recall.sh prints the project brief wiki/projects/<project id>.md first (vault-graph request S9)."""

    def write_brief(self, body: str) -> None:
        brief = self.vault / "wiki" / "projects" / "sample-app.md"
        brief.parent.mkdir(parents=True)
        brief.write_text(
            "---\ntitle: \"Sample App Brief\"\ntype: project\nproject: sample-app\n---\n\n# Sample App Brief\n\n" + body,
            encoding="utf-8",
        )

    def test_brief_comes_first_without_a_section_argument(self):
        self.write_map({"sample-app": ["personal"]})
        self.write_brief("Goal: ship v1.\n\nNext: write the parser.\n")
        result = self.recall()
        self.assertEqual(0, result.returncode, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual("## Project Brief", lines[2])
        self.assertIn("Goal: ship v1.", result.stdout)
        self.assertLess(result.stdout.index("Next: write the parser."), result.stdout.index("## Open Work"))
        self.assertNotIn("# Sample App Brief", result.stdout)

    def test_section_argument_leaves_the_brief_out(self):
        self.write_map({"sample-app": ["personal"]})
        self.write_brief("Goal: ship v1.\n")
        result = self.recall("open")
        self.assertNotIn("Project Brief", result.stdout)

    def test_long_brief_is_cut_by_whole_lines(self):
        self.write_map({"sample-app": ["personal"]})
        self.write_brief("".join(f"Line {index} of the brief with some words in it.\n" for index in range(200)))
        result = self.recall()
        self.assertIn("Line 0 of the brief", result.stdout)
        self.assertNotIn("Line 199 of the brief", result.stdout)

    def test_without_brief_output_has_no_brief_heading(self):
        self.write_map({"sample-app": ["personal"]})
        self.assertNotIn("Project Brief", self.recall().stdout)


if __name__ == "__main__":
    unittest.main()
