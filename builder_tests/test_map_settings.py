"""set-default-write and set-role edit the link config without hand edits (contract 5, A-15).

Every test runs with a temporary HOME and without CLAUDE_CONFIG_DIR, so the real link config is never read
or written.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.vault_migration import installing, map_settings

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools" / "vault_migration" / "cli.py"
MAP_SETTINGS_SOURCE = ROOT / "tools" / "vault_migration" / "map_settings.py"


class _MapCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.vault_map = self.home / ".claude" / "vault-builder" / "vault-map.json"
        self.vault_map.parent.mkdir(parents=True)
        (self.tmp / "vault-a").mkdir()
        (self.tmp / "vault-t").mkdir()
        self.initial = {
            "version": 2,
            "owner_note": "kept",
            "vaults": {
                "a": {"path": str(self.tmp / "vault-a"), "role": "personal", "color": "blue"},
                "t": {"path": str(self.tmp / "vault-t"), "role": "team"},
            },
            "projects": {
                "r": {"root": str(self.tmp / "r"), "vaults": ["a", "t"], "remote": "git@h:o/r.git", "extra": 1},
                "s": {"root": str(self.tmp / "s"), "vaults": ["a"]},
            },
        }
        self.write_map(self.initial)

    def write_map(self, data: dict) -> None:
        self.vault_map.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def read_map(self) -> dict:
        return json.loads(self.vault_map.read_text(encoding="utf-8"))

    def digest(self) -> str:
        return hashlib.sha256(self.vault_map.read_bytes()).hexdigest()

    def env(self) -> dict[str, str]:
        environment = {key: value for key, value in os.environ.items() if key != "CLAUDE_CONFIG_DIR"}
        environment["HOME"] = str(self.home)
        return environment

    def cli(self, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=cwd or ROOT,
            env=self.env(),
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
        )


class SetDefaultWriteTests(_MapCase):
    def test_set_default_write_adds_field_and_keeps_every_other_field(self) -> None:
        result = self.cli("set-default-write", "--project", "r", "--vault", "t")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        expected = json.loads(json.dumps(self.initial))
        expected["projects"]["r"]["default_write_vault"] = "t"
        self.assertEqual(expected, self.read_map())
        self.assertIn("t", result.stdout)

    def test_set_default_write_again_keeps_bytes(self) -> None:
        self.assertEqual(0, self.cli("set-default-write", "--project", "r", "--vault", "t").returncode)
        before = self.digest()
        result = self.cli("set-default-write", "--project", "r", "--vault", "t")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, self.digest())
        self.assertIn("unchanged", result.stdout)

    def test_set_default_write_writes_a_temporary_file_then_renames_it(self) -> None:
        with (
            mock.patch.object(installing.os, "replace", wraps=os.replace) as replace,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = map_settings.set_default_write("r", vault="t", environ=self.env())
        self.assertEqual(0, code)
        self.assertEqual(1, replace.call_count)
        source, target = (Path(item) for item in replace.call_args.args)
        self.assertEqual(self.vault_map, target)
        self.assertEqual(self.vault_map.parent, source.parent)
        self.assertNotEqual(self.vault_map.name, source.name)
        self.assertEqual("t", self.read_map()["projects"]["r"]["default_write_vault"])

    def test_set_default_write_refuses_vault_outside_the_project(self) -> None:
        before = self.digest()
        result = self.cli("set-default-write", "--project", "s", "--vault", "t")
        self.assertEqual(1, result.returncode)
        self.assertIn("s", result.stderr)
        self.assertIn("link", result.stderr)
        self.assertEqual(before, self.digest())

    def test_set_default_write_refuses_unknown_project(self) -> None:
        before = self.digest()
        result = self.cli("set-default-write", "--project", "zzz", "--vault", "a")
        self.assertEqual(1, result.returncode)
        self.assertIn("zzz", result.stderr)
        self.assertIn("r, s", result.stderr)
        self.assertEqual(before, self.digest())

    def test_set_default_write_clear_removes_only_that_field(self) -> None:
        self.assertEqual(0, self.cli("set-default-write", "--project", "r", "--vault", "t").returncode)
        result = self.cli("set-default-write", "--project", "r", "--clear")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.initial, self.read_map())

    def test_set_default_write_clear_without_field_keeps_bytes(self) -> None:
        before = self.digest()
        result = self.cli("set-default-write", "--project", "r", "--clear")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, self.digest())

    def test_set_default_write_needs_exactly_one_of_vault_and_clear(self) -> None:
        before = self.digest()
        for arguments in ((), ("--vault", "a", "--clear")):
            with self.subTest(arguments=arguments):
                result = self.cli("set-default-write", "--project", "r", *arguments)
                self.assertEqual(1, result.returncode)
                self.assertEqual(before, self.digest())

    def test_set_default_write_damaged_map_is_refused(self) -> None:
        self.vault_map.write_text("{", encoding="utf-8")
        result = self.cli("set-default-write", "--project", "r", "--vault", "a")
        self.assertEqual(1, result.returncode)
        self.assertIn("damaged", result.stderr)
        self.assertEqual("{", self.vault_map.read_text(encoding="utf-8"))


class SetRoleTests(_MapCase):
    def test_set_role_changes_role_and_keeps_every_other_field(self) -> None:
        result = self.cli("set-role", "--vault", "a", "--role", "project")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        expected = json.loads(json.dumps(self.initial))
        expected["vaults"]["a"]["role"] = "project"
        self.assertEqual(expected, self.read_map())

    def test_set_role_accepts_a_role_outside_the_documented_values(self) -> None:
        result = self.cli("set-role", "--vault", "t", "--role", "archive")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("archive", self.read_map()["vaults"]["t"]["role"])

    def test_set_role_writes_a_temporary_file_then_renames_it(self) -> None:
        with (
            mock.patch.object(installing.os, "replace", wraps=os.replace) as replace,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = map_settings.set_role("a", "team", environ=self.env())
        self.assertEqual(0, code)
        self.assertEqual(1, replace.call_count)
        source, target = (Path(item) for item in replace.call_args.args)
        self.assertEqual(self.vault_map, target)
        self.assertEqual(self.vault_map.parent, source.parent)
        self.assertEqual("team", self.read_map()["vaults"]["a"]["role"])

    def test_set_role_same_role_keeps_bytes(self) -> None:
        before = self.digest()
        result = self.cli("set-role", "--vault", "t", "--role", "team")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, self.digest())
        self.assertIn("unchanged", result.stdout)

    def test_set_role_refuses_unknown_vault_and_empty_role(self) -> None:
        before = self.digest()
        for arguments, expected in ((("--vault", "zzz", "--role", "team"), "a, t"), (("--vault", "a", "--role", " "), "role")):
            with self.subTest(arguments=arguments):
                result = self.cli("set-role", *arguments)
                self.assertEqual(1, result.returncode)
                self.assertIn(expected, result.stderr)
                self.assertEqual(before, self.digest())


class LinkKeepsDefaultWriteTests(_MapCase):
    def _repo(self, name: str) -> Path:
        repo = self.tmp / name
        repo.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=self.env())
        return repo

    def setUp(self) -> None:
        super().setUp()
        self.repo = self._repo("r")
        self.initial["projects"]["r"]["default_write_vault"] = "t"
        self.initial["projects"]["r"]["vaults"] = ["a", "t"]
        self.write_map(self.initial)

    def test_link_keeps_default_write_vault(self) -> None:
        (self.tmp / "vault-c").mkdir()
        data = self.read_map()
        data["vaults"]["c"] = {"path": str(self.tmp / "vault-c"), "role": "personal"}
        self.write_map(data)
        result = self.cli("link", "c", "--repo", str(self.repo))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        entry = self.read_map()["projects"]["r"]
        self.assertEqual(["a", "t", "c"], entry["vaults"])
        self.assertEqual("t", entry["default_write_vault"])
        self.assertEqual(1, entry["extra"])

    def test_unlink_default_write_vault_clears_the_field(self) -> None:
        result = self.cli("unlink", "t", "--repo", str(self.repo))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        entry = self.read_map()["projects"]["r"]
        self.assertEqual(["a"], entry["vaults"])
        self.assertNotIn("default_write_vault", entry)
        self.assertIn("default", result.stdout)

    def test_unlink_other_vault_keeps_the_field(self) -> None:
        result = self.cli("unlink", "a", "--repo", str(self.repo))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        entry = self.read_map()["projects"]["r"]
        self.assertEqual(["t"], entry["vaults"])
        self.assertEqual("t", entry["default_write_vault"])


class InstallKeepsDefaultWriteTests(_MapCase):
    def test_install_new_vault_keeps_default_write_vault(self) -> None:
        data = self.read_map()
        data["projects"]["r"]["default_write_vault"] = "t"
        self.write_map(data)
        result = self.cli("install", str(self.tmp / "vault-new"), "--name", "new")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        after = self.read_map()
        self.assertIn("new", after["vaults"])
        self.assertEqual("t", after["projects"]["r"]["default_write_vault"])
        self.assertEqual(data["projects"], after["projects"])
        self.assertEqual("kept", after["owner_note"])
        self.assertEqual("blue", after["vaults"]["a"]["color"])

    def test_install_plan_map_keeps_default_write_vault(self) -> None:
        # The map install would write, without the copy and self-check steps.
        data = self.read_map()
        data["projects"]["r"]["default_write_vault"] = "t"
        self.write_map(data)
        plan = installing._precheck(
            self.tmp / "vault-new",
            name="new",
            with_stenc=False,
            builder_root=ROOT,
            environ=self.env(),
            python=sys.executable,
        )
        self.assertEqual("add", plan.vault_map_action)
        self.assertEqual(data["projects"], plan.vault_map["projects"])
        self.assertEqual("t", plan.vault_map["projects"]["r"]["default_write_vault"])
        self.assertEqual("kept", plan.vault_map["owner_note"])
        self.assertEqual("blue", plan.vault_map["vaults"]["a"]["color"])


class MapSettingsImportTests(unittest.TestCase):
    def test_map_settings_does_not_import_hooks(self) -> None:
        tree = ast.parse(MAP_SETTINGS_SOURCE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{alias.name}" for alias in node.names]
            for name in names:
                self.assertNotIn("hooks", name.split("."), name)


if __name__ == "__main__":
    unittest.main()
