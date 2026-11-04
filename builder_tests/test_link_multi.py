"""link with several vault names or none (spec 4.5, decisions 7 and 9, A-8, A-9, A-12, A-14).

Every test runs with a temporary HOME and without CLAUDE_CONFIG_DIR, so the real link config is never read
or written.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.vault_migration import linking

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools" / "vault_migration" / "cli.py"


class _LinkCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.vault_map = self.home / ".claude" / "vault-builder" / "vault-map.json"
        self.vault_map.parent.mkdir(parents=True)
        vaults = {}
        for name, role in (("personal", "personal"), ("team", "team"), ("app", "project"), ("other", "project")):
            (self.tmp / f"vault-{name}").mkdir()
            vaults[name] = {"path": str(self.tmp / f"vault-{name}"), "role": role}
        self.vault_map.write_text(
            json.dumps({"version": 2, "vaults": vaults, "projects": {}}, indent=2) + "\n", encoding="utf-8"
        )
        self.repo = self.tmp / "w"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=self.env())

    def env(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment["HOME"] = str(self.home)
        environment.pop("CLAUDE_CONFIG_DIR", None)
        return environment

    def cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=ROOT,
            env=self.env(),
            text=True,
            capture_output=True,
            check=False,
        )

    def read_map(self) -> dict:
        return json.loads(self.vault_map.read_text(encoding="utf-8"))

    def add_vault(self, name: str, role: str) -> None:
        data = self.read_map()
        (self.tmp / f"vault-{name}").mkdir()
        data["vaults"][name] = {"path": str(self.tmp / f"vault-{name}"), "role": role}
        self.vault_map.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


class LinkManyNamesTests(_LinkCase):
    def test_three_names_link_all_with_one_read_and_one_write(self) -> None:
        router = linking._router()
        stdout = io.StringIO()
        with (
            mock.patch.object(router, "read_vault_map", wraps=router.read_vault_map) as reads,
            mock.patch.object(linking, "_atomic_write", wraps=linking._atomic_write) as writes,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = linking.link(["personal", "team", "app"], repo=str(self.repo), environ=self.env())
        self.assertEqual(0, code, stdout.getvalue())
        self.assertEqual(1, reads.call_count)
        self.assertEqual(1, writes.call_count)
        entry = self.read_map()["projects"]["w"]
        self.assertEqual(["personal", "team", "app"], entry["vaults"])

    def test_default_write_rule_runs_once_after_all_names(self) -> None:
        # With app first, a rule applied per name would see one vault and set nothing, then set app later.
        # Applied once at the end, it sets app and prints the line once.
        result = self.cli("link", "app", "personal", "team", "--repo", str(self.repo))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("app", self.read_map()["projects"]["w"]["default_write_vault"])
        self.assertEqual(1, result.stdout.count("default write vault for project w: app"))

    def test_unknown_name_refuses_before_any_write(self) -> None:
        before = self.vault_map.read_bytes()
        result = self.cli("link", "personal", "zzz", "--repo", str(self.repo))
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("zzz", result.stderr)
        self.assertEqual(before, self.vault_map.read_bytes())

    def test_personal_and_team_leave_default_write_unset(self) -> None:
        result = self.cli("link", "personal", "team", "--repo", str(self.repo))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("default_write_vault", self.read_map()["projects"]["w"])

    def test_single_name_output_is_unchanged(self) -> None:
        result = self.cli("link", "personal", "--repo", str(self.repo))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(
            ["link: project w (created) vaults: personal", f"link: wrote {self.vault_map}"],
            result.stdout.splitlines(),
        )


class LinkWithoutNamesTests(_LinkCase):
    def test_links_every_shared_vault_and_points_at_project_vaults(self) -> None:
        self.add_vault("client", "client")
        result = self.cli("link", "--repo", str(self.repo))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        entry = self.read_map()["projects"]["w"]
        self.assertEqual(["client", "personal", "team"], entry["vaults"])
        self.assertNotIn("default_write_vault", entry)
        hints = [line for line in result.stdout.splitlines() if "vault-builder link <name>" in line]
        self.assertEqual(1, len(hints), result.stdout)
        self.assertIn(f"--repo {self.repo}", hints[0])
        self.assertIn("app, other", hints[0])

    def test_no_project_hint_when_the_repository_has_a_project_vault(self) -> None:
        self.assertEqual(0, self.cli("link", "app", "--repo", str(self.repo)).returncode)
        result = self.cli("link", "--repo", str(self.repo))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(["app", "personal", "team"], self.read_map()["projects"]["w"]["vaults"])
        self.assertNotIn("vault-builder link <name>", result.stdout)

    def test_refuses_when_there_is_no_shared_vault(self) -> None:
        data = self.read_map()
        for name in ("personal", "team"):
            del data["vaults"][name]
        self.vault_map.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        before = self.vault_map.read_bytes()
        result = self.cli("link", "--repo", str(self.repo))
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("vault-builder link <name>", result.stderr)
        self.assertEqual(before, self.vault_map.read_bytes())


class UnknownRoleTests(_LinkCase):
    def test_unknown_role_vault_is_linked_as_shared_with_a_note(self) -> None:
        self.add_vault("client", "client")
        result = self.cli("link", "client", "--repo", str(self.repo))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(["client"], self.read_map()["projects"]["w"]["vaults"])
        self.assertIn(
            "link: vault client has role client, which is not a known role. It is linked as a shared vault.",
            result.stdout.splitlines(),
        )


if __name__ == "__main__":
    unittest.main()
