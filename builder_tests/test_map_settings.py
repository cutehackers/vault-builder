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

from builder_tests._tiers import full_tier_only
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
        # r links a and t, and a is now its only role project vault (DefaultWriteFromProjectRoleTests).
        expected["projects"]["r"]["default_write_vault"] = "a"
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
    @full_tier_only
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


class InstallRoleTests(_MapCase):
    """install --role sets the vault role. Without it a new vault is personal and an existing role is kept."""

    def install(self, name: str, *, role: str | None = None, dry_run: bool = False) -> str:
        out = io.StringIO()
        code = installing.install(
            self.tmp / f"vault-{name}",
            name=name,
            role=role,
            dry_run=dry_run,
            environ=self.env(),
            python=sys.executable,
            out=out,
        )
        self.assertEqual(0, code, out.getvalue())
        return out.getvalue()

    def test_new_vault_without_role_is_personal(self) -> None:
        self.install("new")
        self.assertEqual("personal", self.read_map()["vaults"]["new"]["role"])

    @full_tier_only
    def test_new_vault_gets_the_given_role(self) -> None:
        result = self.cli("install", str(self.tmp / "vault-new"), "--name", "new", "--role", "project")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual({"path": str(self.tmp / "vault-new"), "role": "project"}, self.read_map()["vaults"]["new"])

    def test_existing_vault_keeps_its_role_without_role_option(self) -> None:
        self.install("new", role="project")
        self.install("new")
        self.assertEqual("project", self.read_map()["vaults"]["new"]["role"])

    def test_existing_vault_role_changes_with_role_option_and_keeps_other_fields(self) -> None:
        self.install("new", role="project")
        data = self.read_map()
        data["vaults"]["new"]["color"] = "green"
        self.write_map(data)
        self.install("new", role="team")
        self.assertEqual(
            {"path": str(self.tmp / "vault-new"), "role": "team", "color": "green"}, self.read_map()["vaults"]["new"]
        )

    def test_dry_run_shows_the_role_and_writes_nothing(self) -> None:
        before = self.digest()
        output = self.install("new", role="project", dry_run=True)
        self.assertIn("Vault role: project", output)
        self.assertEqual(before, self.digest())
        self.install("new", role="project")
        self.assertIn("Vault role: project", self.install("new", dry_run=True))
        self.assertIn("Vault role: team", self.install("new", role="team", dry_run=True))
        self.assertEqual("project", self.read_map()["vaults"]["new"]["role"])

    @full_tier_only
    def test_empty_role_is_refused(self) -> None:
        before = self.digest()
        result = self.cli("install", str(self.tmp / "vault-new"), "--name", "new", "--role", " ")
        self.assertEqual(1, result.returncode)
        self.assertIn("role", result.stderr)
        self.assertEqual(before, self.digest())
        self.assertFalse((self.tmp / "vault-new").exists())


class LinkProjectRoleWarningTests(_MapCase):
    """link warns when the project has no role project vault, because session capture needs one."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "w"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=self.env())

    def test_link_without_project_vault_warns_and_exits_zero(self) -> None:
        result = self.cli("link", "a", "--repo", str(self.repo))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        warnings = [line for line in result.stderr.splitlines() if "session transcripts" in line]
        self.assertEqual(1, len(warnings), result.stderr)
        self.assertIn("set-role", warnings[0])
        self.assertEqual(["a"], self.read_map()["projects"]["w"]["vaults"])

    def test_link_with_project_vault_does_not_warn(self) -> None:
        data = self.read_map()
        data["vaults"]["t"]["role"] = "project"
        self.write_map(data)
        self.assertEqual(0, self.cli("link", "a", "--repo", str(self.repo)).returncode)
        result = self.cli("link", "t", "--repo", str(self.repo))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("session transcripts", result.stdout + result.stderr)


class DefaultWriteFromProjectRoleTests(_MapCase):
    """link and set-role pick the default write vault when exactly one linked vault has role project."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "w"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=self.env())
        (self.tmp / "vault-p").mkdir()
        (self.tmp / "vault-q").mkdir()
        data = self.read_map()
        data["vaults"]["p"] = {"path": str(self.tmp / "vault-p"), "role": "project"}
        data["vaults"]["q"] = {"path": str(self.tmp / "vault-q"), "role": "project"}
        self.write_map(data)

    def _link(self, *names: str) -> subprocess.CompletedProcess[str]:
        result = None
        for name in names:
            result = self.cli("link", name, "--repo", str(self.repo))
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def test_link_sets_the_only_project_vault_as_default_write(self) -> None:
        result = self._link("a", "p")
        self.assertEqual("p", self.read_map()["projects"]["w"]["default_write_vault"])
        self.assertIn(
            "link: default write vault for project w: p. Change it with set-default-write --project w --vault <name>.",
            result.stdout,
        )

    def test_link_keeps_an_existing_default_write_vault(self) -> None:
        self._link("a")
        data = self.read_map()
        data["projects"]["w"]["default_write_vault"] = "a"
        self.write_map(data)
        result = self._link("p")
        self.assertEqual("a", self.read_map()["projects"]["w"]["default_write_vault"])
        self.assertNotIn("default write vault", result.stdout)

    def test_link_without_a_project_vault_sets_nothing(self) -> None:
        self._link("a", "t")
        self.assertNotIn("default_write_vault", self.read_map()["projects"]["w"])

    def test_link_with_two_project_vaults_sets_nothing(self) -> None:
        self._link("p", "q")
        self.assertNotIn("default_write_vault", self.read_map()["projects"]["w"])

    def test_link_with_one_vault_sets_nothing(self) -> None:
        self._link("p")
        self.assertNotIn("default_write_vault", self.read_map()["projects"]["w"])

    def test_link_dry_run_does_not_write_the_default(self) -> None:
        self._link("a")
        before = self.digest()
        result = self.cli("link", "p", "--repo", str(self.repo), "--dry-run")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("default write vault for project w: p", result.stdout)
        self.assertEqual(before, self.digest())

    def test_set_role_project_sets_default_write_for_linked_projects(self) -> None:
        result = self.cli("set-role", "--vault", "t", "--role", "project")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        projects = self.read_map()["projects"]
        self.assertEqual("t", projects["r"]["default_write_vault"])
        self.assertNotIn("default_write_vault", projects["s"])
        self.assertIn("set-role: default write vault for project r: t.", result.stdout)

    def test_set_role_keeps_an_existing_default_write_vault(self) -> None:
        data = self.read_map()
        data["projects"]["r"]["default_write_vault"] = "a"
        self.write_map(data)
        result = self.cli("set-role", "--vault", "t", "--role", "project")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("a", self.read_map()["projects"]["r"]["default_write_vault"])


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
