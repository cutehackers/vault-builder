"""import-source copies a file from outside raw/ into raw/imports/<kind>/ and registers it (vault-graph request S8).

The copy keeps the bytes, gets a fixed name, and gets a source page. A file whose sha256 a source page
already records creates nothing and returns the existing paths (vault-graph spec 4.4.5).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_wiki_tools import _project_fixture_vault  # noqa: E402

NOTE = "---\nname: rule\ntype: feedback\n---\n\nAlways run the gate before commit.\n"


class ImportSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.base = Path(tmpdir.name).resolve()
        self.root = self.base / "vault"
        self.root.mkdir()
        _project_fixture_vault(self.root)
        self.note = self.base / "outside" / "feedback_gate.md"
        self.note.parent.mkdir()
        self.note.write_bytes(NOTE.encode("utf-8"))
        self.digest = hashlib.sha256(NOTE.encode("utf-8")).hexdigest()

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        from tools.wiki import cli

        original = cli.ROOT
        out, err = io.StringIO(), io.StringIO()
        try:
            cli.ROOT = self.root
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(list(args))
                except SystemExit as exc:
                    code = int(exc.code)
        finally:
            cli.ROOT = original
        return code, out.getvalue(), err.getvalue()

    def import_note(self, path: Path | None = None, into: str = "raw/imports/claude-memory/") -> tuple[int, dict, str]:
        code, out, err = self.run_cli("import-source", str(path or self.note), "--into", into, "--json")
        return code, json.loads(out) if out.strip() else {}, err

    def files(self) -> dict[Path, bytes]:
        return {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file() and "scratch" not in path.parts}

    def test_copies_bytes_with_fixed_name_and_creates_a_source_page(self):
        from tools.wiki.lib.frontmatter import parse_page
        from tools.wiki.lib.lint import lint_repo

        before_stat = self.note.stat()
        code, result, err = self.import_note()

        self.assertEqual(0, code, err)
        raw = f"raw/imports/claude-memory/feedback_gate--{self.digest[:8]}.md"
        self.assertEqual("imported", result["status"])
        self.assertEqual(raw, result["raw_path"])
        self.assertEqual(self.digest, result["sha256"])
        self.assertEqual(NOTE.encode("utf-8"), (self.root / raw).read_bytes())
        page = parse_page(self.root / result["source_page"])
        self.assertEqual(raw, page.frontmatter["canonical_source"])
        self.assertEqual(self.digest, page.frontmatter["raw_sha256"])
        self.assertEqual([raw, result["source_page"]], result["created"])
        self.assertTrue(result["source_page"].startswith("wiki/sources/"))
        self.assertNotIn(str(self.base), (self.root / result["source_page"]).read_text(encoding="utf-8"))
        self.assertEqual(before_stat.st_mtime_ns, self.note.stat().st_mtime_ns)
        self.assertEqual(NOTE.encode("utf-8"), self.note.read_bytes())
        self.assertTrue(lint_repo(self.root).ok, [issue.message for issue in lint_repo(self.root).issues])

    def test_same_sha256_creates_nothing_and_returns_existing_paths(self):
        _, first, _ = self.import_note()
        before = self.files()
        copy = self.base / "outside" / "renamed.md"
        copy.write_bytes(NOTE.encode("utf-8"))

        for path in (self.note, copy):
            with self.subTest(path=path.name):
                code, result, err = self.import_note(path)
                self.assertEqual(0, code, err)
                self.assertEqual("exists", result["status"])
                self.assertEqual(first["raw_path"], result["raw_path"])
                self.assertEqual(first["source_page"], result["source_page"])
                self.assertEqual([], result["created"])
                self.assertEqual(before, self.files())

    def test_refuses_bad_input_and_writes_nothing(self):
        inside = self.root / "raw" / "sources" / "inside.md"
        inside.parent.mkdir(parents=True, exist_ok=True)
        inside.write_text("inside\n", encoding="utf-8")
        before = self.files()
        cases = [
            (inside, "raw/imports/claude-memory/"),
            (self.base / "missing.md", "raw/imports/claude-memory/"),
            (self.base / "outside", "raw/imports/claude-memory/"),
            (self.note, "raw/sources/"),
            (self.note, "raw/imports/"),
            (self.note, "raw/imports/../sources/"),
            (self.note, "raw/imports/Bad Kind/"),
            (self.note, "wiki/imports/x/"),
        ]
        for path, into in cases:
            with self.subTest(path=path.name, into=into):
                code, result, _ = self.import_note(path, into)
                self.assertEqual(1, code)
                self.assertEqual("error", result["status"])
                self.assertEqual(before, self.files())

    def test_into_without_trailing_slash_is_the_same_folder(self):
        code, result, err = self.import_note(into="raw/imports/claude-memory")
        self.assertEqual(0, code, err)
        self.assertTrue(result["raw_path"].startswith("raw/imports/claude-memory/"))

    def test_text_output_without_json(self):
        code, out, err = self.run_cli("import-source", str(self.note), "--into", "raw/imports/notes/")
        self.assertEqual(0, code, err)
        self.assertIn("raw/imports/notes/", out)


class ImportSourceDocTests(unittest.TestCase):
    def test_agents_and_schema_allow_import_source_under_raw_imports(self):
        for relative in ("AGENTS.md", "docs/agent/OPERATING-SCHEMA.md"):
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("import-source", text)
                self.assertIn("raw/imports/", text)


if __name__ == "__main__":
    unittest.main()
