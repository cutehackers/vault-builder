"""uninstall --all: every Vault in vault-map.json, then the vault-builder command file and the builder checkout.

Spec 4.3 item 3, 4.8, A-23 (vault-builder part), A-33 (removal part). Every test runs with a temporary HOME
and without CLAUDE_CONFIG_DIR, so the real ~/.claude, ~/.claude.json and ~/.local are never read or written.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.vault_migration import builder_command, cli, installing, uninstalling

ROOT = Path(__file__).resolve().parents[1]


class _UninstallAllCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.config = self.home / ".claude"
        patched = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patched.start()
        self.addCleanup(patched.stop)
        for key in ("CLAUDE_CONFIG_DIR", "VAULT_GRAPH_BIN", "VAULT_BUILDER_HOME"):
            os.environ.pop(key, None)

    def env(self, **extra: str) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"CLAUDE_CONFIG_DIR", "VAULT_GRAPH_BIN", "VAULT_BUILDER_HOME"}
        }
        environment["HOME"] = str(self.home)
        environment["PATH"] = os.pathsep.join(["/usr/bin", "/bin"])
        environment.update(extra)
        return environment

    @property
    def vault_map(self) -> Path:
        return self.config / "vault-builder" / "vault-map.json"

    @property
    def command(self) -> Path:
        return builder_command.command_path(self.home)

    @property
    def default_checkout(self) -> Path:
        return self.home / ".local" / "share" / "vault-builder"

    def vault(self, name: str) -> Path:
        return self.work / f"vault-{name}"

    def install(self, name: str, *, with_hooks: bool = True) -> Path:
        target = self.vault(name)
        code = installing.install(
            target, name=name, with_hooks=with_hooks, environ=self.env(), python=sys.executable, out=io.StringIO()
        )
        self.assertEqual(0, code)
        return target

    def fake_checkout(self, path: Path) -> Path:
        (path / builder_command.CLI_RELATIVE).parent.mkdir(parents=True)
        (path / builder_command.CLI_RELATIVE).write_text("# cli\n", encoding="utf-8")
        (path / ".git").mkdir()
        return path

    def write_command(self, checkout: Path) -> None:
        self.command.parent.mkdir(parents=True, exist_ok=True)
        self.command.write_text(builder_command.command_text(checkout, sys.executable), encoding="utf-8")
        self.command.chmod(0o755)

    def uninstall_all(self, builder_root: Path = ROOT, **extra: str) -> tuple[int, str]:
        out = io.StringIO()
        code = uninstalling.uninstall_all(builder_root=builder_root, environ=self.env(**extra), out=out)
        return code, out.getvalue()


class EveryVaultTests(_UninstallAllCase):
    def test_every_vault_is_uninstalled_and_knowledge_stays(self) -> None:
        first = self.install("a")
        second = self.install("b")
        (first / "raw" / "sources" / "note.md").write_text("Raw note.\n", encoding="utf-8")
        settings = self.config / "settings.json"
        self.assertIn("vault_hooks.py", settings.read_text(encoding="utf-8"))

        code, output = self.uninstall_all()

        self.assertEqual(0, code, output)
        for target in (first, second):
            self.assertFalse((target / ".vault-builder").exists(), output)
            self.assertTrue((target / "raw").is_dir())
            self.assertTrue((target / "wiki" / "index.md").is_file())
        self.assertEqual("Raw note.\n", (first / "raw" / "sources" / "note.md").read_text(encoding="utf-8"))
        self.assertFalse(self.vault_map.exists())
        self.assertFalse((self.config / "skills" / "project-memory").exists())
        self.assertFalse((self.config / "vault-builder").exists())
        self.assertNotIn("vault_hooks.py", settings.read_text(encoding="utf-8"))
        claude = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))
        self.assertNotIn("llm-wiki", claude.get("mcpServers", {}))
        self.assertIn(f"Uninstalled {first}", output)
        self.assertIn(f"Uninstalled {second}", output)

    def test_a_failing_vault_stops_with_its_name_and_error(self) -> None:
        self.install("a")
        second = self.install("b")
        (second / ".vault-builder" / "manifest.json").write_text("not json\n", encoding="utf-8")
        checkout = self.fake_checkout(self.default_checkout)
        self.write_command(checkout)
        expected = ""
        try:
            uninstalling.uninstall(second, environ=self.env(), out=io.StringIO())
        except uninstalling.UninstallError as exc:
            expected = str(exc)
        self.assertTrue(expected)

        with self.assertRaises(uninstalling.UninstallError) as caught:
            self.uninstall_all()

        message = str(caught.exception)
        self.assertIn("b", message)
        self.assertIn(str(second), message)
        self.assertIn(expected, message)
        self.assertTrue(self.command.is_file(), "the command file stays when a vault fails")
        self.assertTrue(checkout.is_dir(), "the checkout stays when a vault fails")
        vault_map = json.loads(self.vault_map.read_text(encoding="utf-8"))
        self.assertEqual(["b"], list(vault_map["vaults"]))

    def test_without_a_vault_map_it_still_removes_the_command(self) -> None:
        self.write_command(self.fake_checkout(self.default_checkout))

        code, output = self.uninstall_all()

        self.assertEqual(0, code, output)
        self.assertFalse(self.command.exists())
        self.assertIn("vault map", output)


class CommandAndCheckoutTests(_UninstallAllCase):
    def test_a_marked_command_and_the_default_checkout_are_deleted(self) -> None:
        self.install("a")
        checkout = self.fake_checkout(self.default_checkout)
        self.write_command(checkout)

        code, output = self.uninstall_all()

        self.assertEqual(0, code, output)
        self.assertFalse(self.command.exists())
        self.assertFalse(checkout.exists())
        self.assertTrue((self.home / ".local" / "bin").is_dir(), "the bin folder stays")
        self.assertIn(f"deleted {self.command}", output)
        self.assertTrue(output.rstrip("\n").endswith(f"deleting {checkout} (last step)"), output)

    def test_a_file_without_the_marker_is_kept(self) -> None:
        checkout = self.fake_checkout(self.default_checkout)
        self.command.parent.mkdir(parents=True)
        mine = f"#!/bin/sh\nVAULT_BUILDER_CHECKOUT={checkout}\nexec my-own-tool\n"
        self.command.write_text(mine, encoding="utf-8")

        code, output = self.uninstall_all()

        self.assertEqual(0, code, output)
        self.assertEqual(mine, self.command.read_text(encoding="utf-8"))
        self.assertTrue(checkout.is_dir())
        self.assertIn(f"Kept {self.command}", output)

    def test_a_checkout_the_user_chose_is_kept(self) -> None:
        chosen = self.fake_checkout(self.tmp / "my-builder")
        self.write_command(chosen)

        code, output = self.uninstall_all(VAULT_BUILDER_HOME=str(chosen))

        self.assertEqual(0, code, output)
        self.assertFalse(self.command.exists())
        self.assertTrue((chosen / builder_command.CLI_RELATIVE).is_file())
        self.assertIn(f"Kept the builder checkout {chosen}", output)

    def test_a_chosen_checkout_is_kept_even_without_the_variable(self) -> None:
        chosen = self.fake_checkout(self.tmp / "my-builder")
        self.write_command(chosen)

        code, output = self.uninstall_all()

        self.assertEqual(0, code, output)
        self.assertTrue(chosen.is_dir())

    def test_the_running_checkout_is_deleted_as_the_last_step(self) -> None:
        checkout = self.fake_checkout(self.default_checkout)
        self.write_command(checkout)

        # builder_root is the checkout this code runs from; point it at the fake default checkout.
        code, output = self.uninstall_all(builder_root=checkout)

        self.assertEqual(0, code, output)
        self.assertFalse(self.command.exists())
        self.assertFalse(checkout.exists())
        self.assertTrue(output.rstrip("\n").endswith(f"deleting {checkout} (last step)"), output)

    def test_a_failed_checkout_deletion_is_kept_with_a_command_and_exits_zero(self) -> None:
        checkout = self.fake_checkout(self.default_checkout)
        self.write_command(checkout)

        with mock.patch.object(uninstalling.shutil, "rmtree", side_effect=PermissionError("denied")):
            code, output = self.uninstall_all()

        self.assertEqual(0, code, output)
        self.assertTrue(checkout.is_dir())
        self.assertIn(f"Kept the builder checkout {checkout}: denied. Remove it with: rm -rf {checkout}", output)

    def test_a_default_folder_that_is_not_a_checkout_is_kept(self) -> None:
        folder = self.default_checkout
        folder.mkdir(parents=True)
        (folder / "notes.txt").write_text("mine\n", encoding="utf-8")
        self.write_command(folder)

        code, output = self.uninstall_all()

        self.assertEqual(0, code, output)
        self.assertTrue((folder / "notes.txt").is_file())

    def test_dry_run_writes_nothing(self) -> None:
        target = self.install("a")
        checkout = self.fake_checkout(self.default_checkout)
        self.write_command(checkout)
        out = io.StringIO()

        code = uninstalling.uninstall_all(dry_run=True, environ=self.env(), out=out)

        self.assertEqual(0, code, out.getvalue())
        self.assertTrue((target / ".vault-builder" / "manifest.json").is_file())
        self.assertTrue(self.command.is_file())
        self.assertTrue(checkout.is_dir())
        self.assertIn(f"delete {self.command}", out.getvalue())
        self.assertIn(f"delete {checkout}", out.getvalue())


class CliTests(_UninstallAllCase):
    def test_uninstall_all_runs_from_the_cli(self) -> None:
        target = self.install("a")
        self.write_command(self.fake_checkout(self.default_checkout))

        with mock.patch("sys.stdout", new_callable=io.StringIO):
            code = cli.main(["uninstall", "--all"])

        self.assertEqual(0, code)
        self.assertFalse((target / ".vault-builder").exists())
        self.assertFalse(self.command.exists())

    def test_a_failing_vault_exits_one_from_the_cli(self) -> None:
        target = self.install("a")
        (target / ".vault-builder" / "manifest.json").write_text("not json\n", encoding="utf-8")

        with mock.patch("sys.stdout", new_callable=io.StringIO), mock.patch(
            "sys.stderr", new_callable=io.StringIO
        ) as stderr:
            code = cli.main(["uninstall", "--all"])

        self.assertEqual(1, code)
        self.assertIn(str(target), stderr.getvalue())

    def test_path_and_all_are_exclusive_and_one_is_required(self) -> None:
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(1, cli.main(["uninstall"]))
            self.assertEqual(1, cli.main(["uninstall", str(self.work), "--all"]))


if __name__ == "__main__":
    unittest.main()
