"""install --with-hooks and uninstall manage the user-scope Claude Code hook entries (contract 6, A-16).

Every test runs with a temporary HOME and without CLAUDE_CONFIG_DIR unless the test sets one inside the
temporary HOME, so the real settings.json is never read or written.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.vault_migration import cli, hooks, installing, uninstalling

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_ASSET = ROOT / "tools" / "vault_migration" / "assets" / "vault-hooks" / "vault_hooks.py"
ROUTER_ASSET = ROOT / "tools" / "vault_migration" / "assets" / "llm-wiki-router" / "llm_wiki_router.py"
EVENTS = {"SessionStart": "session-start", "PreCompact": "capture", "SessionEnd": "capture"}


class _HooksCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.config = self.home / ".claude"
        patched = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patched.start()
        self.addCleanup(patched.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("VAULT_GRAPH_BIN", None)
        self.python = sys.executable

    @property
    def settings(self) -> Path:
        return self.config / "settings.json"

    @property
    def launcher(self) -> Path:
        return self.config / "vault-builder" / "vault_hooks.py"

    def env(self, **extra: str) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"CLAUDE_CONFIG_DIR", "VAULT_GRAPH_BIN"}
        }
        environment["HOME"] = str(self.home)
        environment["PATH"] = os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])
        environment.update(extra)
        return environment

    def fake_vg(self) -> Path:
        vg = self.bin / "vg"
        vg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        vg.chmod(0o755)
        return vg

    def install(self, name: str = "a", *, with_hooks: bool = True, **extra: str) -> str:
        out = io.StringIO()
        code = installing.install(
            self.work / f"vault-{name}",
            name=name,
            with_hooks=with_hooks,
            environ=self.env(**extra),
            python=self.python,
            out=out,
        )
        self.assertEqual(0, code)
        return out.getvalue()

    def uninstall(self, name: str = "a", **extra: str) -> str:
        out = io.StringIO()
        code = uninstalling.uninstall(self.work / f"vault-{name}", environ=self.env(**extra), out=out)
        self.assertEqual(0, code)
        return out.getvalue()

    def command(self, subcommand: str, launcher: Path | None = None) -> str:
        return shlex.join([self.python, "-I", str(launcher or self.launcher), subcommand])

    def read_settings(self, path: Path | None = None) -> dict:
        return json.loads((path or self.settings).read_text(encoding="utf-8"))

    def write_settings(self, value: object) -> bytes:
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        text = value if isinstance(value, str) else json.dumps(value, indent=2) + "\n"
        self.settings.write_text(text, encoding="utf-8")
        return self.settings.read_bytes()

    def assert_temporary(self, path: Path) -> None:
        self.assertTrue(path.resolve().is_relative_to(self.tmp), path)


class TemporaryHomeTests(_HooksCase):
    def test_every_hook_path_is_inside_the_temporary_home(self) -> None:
        user = installing.resolve_user_paths(self.env())
        for path in (hooks.settings_path(user.config_dir), hooks.launcher_path(user.config_dir),
                     hooks.hooks_config_path(user.config_dir)):
            self.assert_temporary(path)


class InstallWithHooksTests(_HooksCase):
    def test_adds_three_entries_and_second_run_keeps_settings_bytes(self) -> None:
        self.fake_vg()

        self.install()

        settings = self.read_settings()
        for event, subcommand in EVENTS.items():
            groups = settings["hooks"][event]
            self.assertEqual(1, len(groups), event)
            items = groups[0]["hooks"]
            self.assertEqual(1, len(items), event)
            self.assertEqual("command", items[0]["type"])
            self.assertEqual(self.command(subcommand), items[0]["command"])
        first = self.settings.read_bytes()

        self.install()

        self.assertEqual(first, self.settings.read_bytes())

    def test_capture_entries_have_a_60_second_timeout(self) -> None:
        self.install()

        settings = self.read_settings()
        self.assertEqual(60, settings["hooks"]["PreCompact"][0]["hooks"][0]["timeout"])
        self.assertEqual(60, settings["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"])
        self.assertNotIn("timeout", settings["hooks"]["SessionStart"][0]["hooks"][0])

    def test_copies_the_launcher_next_to_the_router(self) -> None:
        self.install()

        self.assertEqual(LAUNCHER_ASSET.read_bytes(), self.launcher.read_bytes())
        self.assertEqual(ROUTER_ASSET.read_bytes(), (self.launcher.parent / "llm_wiki_router.py").read_bytes())

    def test_records_the_absolute_vg_path(self) -> None:
        vg = self.fake_vg()

        self.install()

        recorded = json.loads((self.config / "vault-builder" / "vault-hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(str(vg), recorded["vg"])
        self.assertTrue(os.path.isabs(recorded["vg"]))

    def test_vault_graph_bin_wins_over_path(self) -> None:
        self.fake_vg()
        chosen = self.tmp / "elsewhere" / "vg"
        chosen.parent.mkdir()
        chosen.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        chosen.chmod(0o755)

        self.install(VAULT_GRAPH_BIN=str(chosen))

        recorded = json.loads((self.config / "vault-builder" / "vault-hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(str(chosen), recorded["vg"])

    def test_without_vg_install_notes_it_and_writes_no_vg_record(self) -> None:
        output = self.install()

        self.assertFalse((self.config / "vault-builder" / "vault-hooks.json").exists())
        self.assertIn("vg was not found", output)
        self.assertTrue(self.settings.is_file())

    def test_a_removed_vg_record_is_deleted_when_no_vg_is_found(self) -> None:
        """After a tool removes its vg, running install again leaves no stale vg record for doctor to report."""
        vg = self.fake_vg()
        self.install()
        config = self.config / "vault-builder" / "vault-hooks.json"
        self.assertTrue(config.is_file())
        vg.unlink()

        self.install()

        self.assertFalse(config.exists())

    def test_a_vg_record_that_still_runs_is_kept_when_vg_is_not_on_path(self) -> None:
        chosen = self.tmp / "elsewhere" / "vg"
        chosen.parent.mkdir()
        chosen.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        chosen.chmod(0o755)
        self.install(VAULT_GRAPH_BIN=str(chosen))
        config = self.config / "vault-builder" / "vault-hooks.json"
        before = config.read_bytes()

        self.install()

        self.assertEqual(before, config.read_bytes())

    def test_keeps_other_hook_entries_and_settings(self) -> None:
        other_start = {"matcher": "startup", "hooks": [{"type": "command", "command": "other-tool start"}]}
        other_tool = {"matcher": "Bash", "hooks": [{"type": "command", "command": "lint-bash"}]}
        self.write_settings({"model": "opus", "hooks": {"SessionStart": [other_start], "PreToolUse": [other_tool]}})

        self.install()

        settings = self.read_settings()
        self.assertEqual("opus", settings["model"])
        self.assertEqual(other_start, settings["hooks"]["SessionStart"][0])
        self.assertEqual(2, len(settings["hooks"]["SessionStart"]))
        self.assertEqual([other_tool], settings["hooks"]["PreToolUse"])

    def test_replaces_a_stale_launcher_entry_instead_of_adding_a_second(self) -> None:
        stale = {"hooks": [{"type": "command", "command": shlex.join(["/old/python", "-I", str(self.launcher), "capture"])}]}
        self.write_settings({"hooks": {"PreCompact": [stale]}})

        self.install()

        groups = self.read_settings()["hooks"]["PreCompact"]
        self.assertEqual(1, len(groups))
        self.assertEqual(self.command("capture"), groups[0]["hooks"][0]["command"])

    def test_uses_settings_in_claude_config_dir(self) -> None:
        config = self.home / "custom-config"

        self.install(CLAUDE_CONFIG_DIR=str(config))

        launcher = config / "vault-builder" / "vault_hooks.py"
        settings = self.read_settings(config / "settings.json")
        self.assertEqual(self.command("session-start", launcher), settings["hooks"]["SessionStart"][0]["hooks"][0]["command"])
        self.assertFalse(self.settings.exists())

    def test_settings_that_is_not_json_stops_install_before_any_write(self) -> None:
        before = self.write_settings("{not json")
        home_before = sorted(str(path) for path in self.home.rglob("*"))

        with self.assertRaises(installing.InstallError) as raised:
            self.install()

        self.assertIn("settings.json", str(raised.exception))
        self.assertEqual(before, self.settings.read_bytes())
        self.assertEqual(home_before, sorted(str(path) for path in self.home.rglob("*")))
        self.assertEqual([], list(self.work.iterdir()))

    def test_settings_with_a_non_object_hooks_value_stops_install(self) -> None:
        before = self.write_settings({"hooks": ["not", "an", "object"]})

        with self.assertRaises(installing.InstallError):
            self.install()

        self.assertEqual(before, self.settings.read_bytes())
        self.assertEqual([], list(self.work.iterdir()))

    def test_dry_run_lists_hook_changes_and_writes_nothing(self) -> None:
        out = io.StringIO()

        installing.install(
            self.work / "vault-a", name="a", with_hooks=True, dry_run=True,
            environ=self.env(), python=self.python, out=out,
        )

        self.assertIn("settings.json", out.getvalue())
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.launcher.exists())


class InstallWithoutHooksTests(_HooksCase):
    def test_does_not_read_or_write_settings(self) -> None:
        before = self.write_settings("{not json, and install must not read it")

        with mock.patch.object(hooks, "load_settings", side_effect=AssertionError("settings.json was read")):
            self.install(with_hooks=False)

        self.assertEqual(before, self.settings.read_bytes())
        self.assertFalse(self.launcher.exists())
        self.assertFalse((self.config / "vault-builder" / "vault-hooks.json").exists())


class UninstallHooksTests(_HooksCase):
    def test_removes_only_launcher_entries(self) -> None:
        self.fake_vg()
        other_start = {"matcher": "startup", "hooks": [{"type": "command", "command": "other-tool start"}]}
        other_end = {"hooks": [{"type": "command", "command": "other-tool end"}]}
        self.write_settings({"model": "opus", "hooks": {"SessionStart": [other_start], "SessionEnd": [other_end]}})
        self.install()

        output = self.uninstall()

        settings = self.read_settings()
        self.assertEqual({"model": "opus", "hooks": {"SessionStart": [other_start], "SessionEnd": [other_end]}}, settings)
        self.assertNotIn(str(self.launcher), self.settings.read_text(encoding="utf-8"))
        self.assertFalse(self.launcher.exists())
        self.assertFalse((self.config / "vault-builder" / "vault-hooks.json").exists())
        self.assertIn("settings.json", output)

    def test_keeps_hooks_while_another_vault_remains(self) -> None:
        self.install("a")
        self.install("b", with_hooks=False)
        before = self.settings.read_bytes()

        self.uninstall("a")

        self.assertEqual(before, self.settings.read_bytes())
        self.assertTrue(self.launcher.exists())
        self.uninstall("b")
        self.assertNotIn(str(self.launcher), self.settings.read_text(encoding="utf-8"))

    def test_without_settings_uninstall_creates_none(self) -> None:
        self.install(with_hooks=False)

        self.uninstall()

        self.assertFalse(self.settings.exists())

    def test_settings_that_is_not_json_is_kept_with_a_note(self) -> None:
        self.install()
        before = self.write_settings("{broken")

        output = self.uninstall()

        self.assertEqual(before, self.settings.read_bytes())
        self.assertIn("not valid JSON", output)

    def test_remove_drops_only_the_launcher_item_from_a_mixed_group(self) -> None:
        launcher = self.launcher
        settings = {
            "hooks": {
                "SessionEnd": [
                    {"hooks": [
                        {"type": "command", "command": "keep-me"},
                        {"type": "command", "command": self.command("capture")},
                    ]},
                ],
                "PreCompact": [{"hooks": [{"type": "command", "command": self.command("capture")}]}],
            }
        }

        result, removed = hooks.remove_launcher_hooks(settings, launcher)

        self.assertEqual(2, removed)
        self.assertEqual({"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "keep-me"}]}]}}, result)


class CliTests(_HooksCase):
    def test_install_accepts_with_hooks(self) -> None:
        os.environ["PATH"] = self.env()["PATH"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(["install", str(self.work / "vault-a"), "--name", "a", "--with-hooks"])

        self.assertEqual(0, code, err.getvalue())
        self.assertIn(str(self.launcher), self.settings.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
