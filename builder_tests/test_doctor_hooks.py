"""doctor without a path, the hook and vg checks, and the vault-builder command file (spec 4.3 item 4, 4.7).

Every test runs with a temporary HOME and without CLAUDE_CONFIG_DIR, so the real ~/.claude and
~/.claude.json are never read or written (A-15, A-16, A-31, A-33).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.vault_migration import builder_command, cli, installing, uninstalling

ROOT = Path(__file__).resolve().parents[1]
EVENTS = ("SessionStart", "PreCompact", "SessionEnd")


def _snapshot(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class _DoctorCase(unittest.TestCase):
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

    @property
    def settings(self) -> Path:
        return self.config / "settings.json"

    @property
    def launcher(self) -> Path:
        return self.config / "vault-builder" / "vault_hooks.py"

    @property
    def hooks_config(self) -> Path:
        return self.config / "vault-builder" / "vault-hooks.json"

    @property
    def claude_json(self) -> Path:
        return self.home / ".claude.json"

    def env(self, **extra: str) -> dict[str, str]:
        environment = {
            key: value for key, value in os.environ.items() if key not in {"CLAUDE_CONFIG_DIR", "VAULT_GRAPH_BIN"}
        }
        environment["HOME"] = str(self.home)
        environment["PATH"] = os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])
        environment.update(extra)
        return environment

    def fake_vg(self, body: str = '[ "$1" = --help ] && exit 0\nexit 2\n') -> Path:
        vg = self.bin / "vg"
        vg.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        vg.chmod(0o755)
        return vg

    def vault(self, name: str) -> Path:
        return self.work / f"vault-{name}"

    def install(self, name: str = "a", *, with_hooks: bool = True) -> None:
        code = installing.install(
            self.vault(name), name=name, with_hooks=with_hooks, environ=self.env(), python=sys.executable,
            out=io.StringIO(),
        )
        self.assertEqual(0, code)

    def doctor(self, target: Path | None = None, **extra: str) -> tuple[int, str]:
        out = io.StringIO()
        code = uninstalling.doctor(target, environ=self.env(**extra), out=out)
        return code, out.getvalue()

    def problems(self, output: str) -> list[str]:
        lines = [line for line in output.splitlines() if line.startswith("Problem:")]
        for line in lines:
            self.assertIn(" Fix: ", line, "every problem line ends with a fix command")
        return lines

    def notes(self, output: str) -> list[str]:
        return [line for line in output.splitlines() if line.startswith("Note:")]

    def assert_one_problem(self, output: str, *needles: str, fix: str = "vault-builder setup") -> str:
        lines = [line for line in self.problems(output) if all(needle in line for needle in needles)]
        self.assertEqual(1, len(lines), output)
        self.assertTrue(lines[0].endswith(f"Fix: {fix}"), lines[0])
        return lines[0]

    def write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def remove_launcher_item(self, event: str) -> None:
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        del settings["hooks"][event]
        self.write_json(self.settings, settings)


class AllVaultsTests(_DoctorCase):
    def test_without_a_path_checks_every_vault_and_shared_items_once(self) -> None:
        self.fake_vg()
        self.install("a")
        self.install("b")

        code, output = self.doctor()

        self.assertEqual(0, code, output)
        self.assertIn("2 vaults", output)
        router = self.config / "vault-builder" / "llm_wiki_router.py"
        router.write_text("# changed\n", encoding="utf-8")
        (self.vault("b") / "AGENTS.md").write_text("changed\n", encoding="utf-8")

        code, output = self.doctor()

        self.assertEqual(1, code, output)
        self.assert_one_problem(output, "Router")
        line = self.assert_one_problem(output, "AGENTS.md", fix=f"vault-builder audit --target {self.vault('b')}")
        self.assertIn("b (", line)

    def test_without_a_path_and_without_a_vault_map_reports_it(self) -> None:
        code, output = self.doctor()

        self.assertEqual(1, code, output)
        self.assert_one_problem(output, "vault-map.json")

    def test_doctor_writes_no_file(self) -> None:
        self.fake_vg()
        self.install("a")
        self.remove_launcher_item("PreCompact")
        before = _snapshot(self.tmp)

        self.doctor()
        self.doctor(self.vault("a"))

        self.assertEqual(before, _snapshot(self.tmp))

    def test_cli_doctor_path_is_optional(self) -> None:
        self.assertIsNone(cli._build_parser().parse_args(["doctor"]).path)
        self.assertEqual("x", cli._build_parser().parse_args(["doctor", "x"]).path)


class HookTests(_DoctorCase):
    def test_full_install_exits_zero(self) -> None:
        self.fake_vg()
        self.install()

        code, output = self.doctor(self.vault("a"))

        self.assertEqual(0, code, output)
        self.assertEqual([], self.problems(output))

    def test_a_missing_launcher_entry_names_the_event(self) -> None:
        self.fake_vg()
        self.install()
        for event in EVENTS:
            with self.subTest(event=event):
                saved = self.settings.read_bytes()
                self.remove_launcher_item(event)
                try:
                    code, output = self.doctor()
                    self.assertEqual(1, code, output)
                    self.assert_one_problem(output, event)
                finally:
                    self.settings.write_bytes(saved)

    def test_a_second_launcher_entry_is_a_problem(self) -> None:
        self.fake_vg()
        self.install()
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        settings["hooks"]["SessionStart"].append(settings["hooks"]["SessionStart"][0])
        self.write_json(self.settings, settings)

        code, output = self.doctor()

        self.assertEqual(1, code, output)
        self.assert_one_problem(output, "SessionStart", "2")

    def test_a_missing_or_changed_launcher_is_a_problem(self) -> None:
        self.fake_vg()
        self.install()
        saved = self.launcher.read_bytes()
        self.launcher.write_text("# changed\n", encoding="utf-8")
        code, output = self.doctor()
        self.assertEqual(1, code, output)
        self.assert_one_problem(output, str(self.launcher))

        self.launcher.unlink()
        code, output = self.doctor()
        self.assertEqual(1, code, output)
        self.assert_one_problem(output, str(self.launcher))
        self.launcher.write_bytes(saved)

    def test_a_vg_path_that_does_not_exist_is_a_problem(self) -> None:
        self.fake_vg()
        self.install()
        for value in (str(self.tmp / "gone" / "vg"), "vg"):
            with self.subTest(vg=value):
                self.write_json(self.hooks_config, {"version": 1, "vg": value})

                code, output = self.doctor()

                self.assertEqual(1, code, output)
                self.assert_one_problem(output, "vg", str(self.hooks_config))

    def test_vg_help_must_exit_zero(self) -> None:
        self.fake_vg()
        self.install()
        self.fake_vg("exit 3\n")

        code, output = self.doctor()

        self.assertEqual(1, code, output)
        self.assert_one_problem(output, "--help", "3")

    def test_vg_help_must_finish_in_time(self) -> None:
        self.fake_vg()
        self.install()
        self.fake_vg("sleep 5\n")

        with mock.patch.object(uninstalling, "_VG_HELP_TIMEOUT_SECONDS", 0.5):
            code, output = self.doctor()

        self.assertEqual(1, code, output)
        self.assert_one_problem(output, "--help", "0.5 seconds")

    def test_without_vault_graph_exits_zero_with_a_note(self) -> None:
        self.install()  # no vg on PATH: install records no vg path

        code, output = self.doctor()

        self.assertEqual(0, code, output)
        self.assertTrue(any("recall.sh" in line for line in self.notes(output)), output)
        self.assertTrue(any("mcpServers.vault-graph" in line for line in self.notes(output)), output)

    def test_without_hooks_exits_zero_with_a_note(self) -> None:
        self.install(with_hooks=False)

        code, output = self.doctor(self.vault("a"))

        self.assertEqual(0, code, output)
        self.assertTrue(any("vault-builder setup" in line and "hook" in line for line in self.notes(output)), output)

    def test_settings_that_is_not_json_is_a_problem(self) -> None:
        self.fake_vg()
        self.install()
        self.settings.write_text("{broken", encoding="utf-8")

        code, output = self.doctor()

        self.assertEqual(1, code, output)
        self.assert_one_problem(output, str(self.settings), fix=f"python3 -m json.tool {self.settings}")


class VaultGraphEntryTests(_DoctorCase):
    def set_entry(self, entry: object) -> None:
        config = json.loads(self.claude_json.read_text(encoding="utf-8"))
        config["mcpServers"]["vault-graph"] = entry
        self.write_json(self.claude_json, config)

    def test_an_executable_command_is_not_reported(self) -> None:
        vg = self.fake_vg()
        self.install()
        self.set_entry({"command": str(vg), "args": ["mcp", "serve"]})

        code, output = self.doctor()

        self.assertEqual(0, code, output)
        self.assertFalse(any("mcpServers.vault-graph" in line for line in output.splitlines()), output)

    def test_a_command_that_does_not_run_is_a_problem(self) -> None:
        self.fake_vg()
        self.install()
        self.set_entry({"command": str(self.tmp / "gone" / "vg"), "args": []})

        code, output = self.doctor()

        self.assertEqual(1, code, output)
        self.assert_one_problem(output, "mcpServers.vault-graph")


class CommandFileTests(_DoctorCase):
    @property
    def command(self) -> Path:
        return self.home / ".local" / "bin" / "vault-builder"

    def write_command(self, text: str) -> None:
        self.command.parent.mkdir(parents=True, exist_ok=True)
        self.command.write_text(text, encoding="utf-8")
        self.command.chmod(0o755)

    def test_text_has_the_marker_and_the_checkout(self) -> None:
        checkout = self.tmp / "a checkout"
        text = builder_command.command_text(checkout, sys.executable).encode("utf-8")

        self.assertTrue(builder_command.has_marker(text))
        self.assertEqual(checkout, builder_command.read_checkout(text))
        self.assertEqual(self.command, builder_command.command_path(self.home))
        self.assertFalse(builder_command.has_marker(b"#!/bin/sh\nexec something\n"))
        self.assertIsNone(builder_command.read_checkout(b"#!/bin/sh\nVAULT_BUILDER_CHECKOUT=/x\n"))
        self.assertIn(shlex.quote(str(checkout)), text.decode("utf-8"))

    def test_a_command_for_this_checkout_is_not_reported(self) -> None:
        self.fake_vg()
        self.install()
        self.write_command(builder_command.command_text(ROOT, sys.executable))

        code, output = self.doctor()

        self.assertEqual(0, code, output)
        self.assertFalse(any(str(self.command) in line for line in output.splitlines()), output)

    def test_a_missing_command_is_a_note(self) -> None:
        self.fake_vg()
        self.install()

        code, output = self.doctor()

        self.assertEqual(0, code, output)
        self.assertTrue(any(str(self.command) in line for line in self.notes(output)), output)

    def test_a_command_for_a_missing_checkout_is_a_problem(self) -> None:
        self.fake_vg()
        self.install()
        self.write_command(builder_command.command_text(self.tmp / "gone", sys.executable))

        code, output = self.doctor()

        self.assertEqual(1, code, output)
        self.assert_one_problem(output, str(self.command), str(self.tmp / "gone"), fix=builder_command.INSTALL_COMMAND)

    def test_a_command_without_the_marker_is_a_problem_and_is_kept(self) -> None:
        self.fake_vg()
        self.install()
        self.write_command("#!/bin/sh\necho mine\n")

        code, output = self.doctor()

        self.assertEqual(1, code, output)
        self.assert_one_problem(
            output, str(self.command), fix=f"mv {self.command} {self.command}.mine && {builder_command.INSTALL_COMMAND}"
        )
        self.assertEqual("#!/bin/sh\necho mine\n", self.command.read_text(encoding="utf-8"))


class InstallHintTests(_DoctorCase):
    def test_install_points_at_vault_builder_doctor(self) -> None:
        out = io.StringIO()
        installing.install(self.vault("a"), name="a", environ=self.env(), python=sys.executable, out=out)

        self.assertIn(f"vault-builder doctor {self.vault('a')}", out.getvalue())


if __name__ == "__main__":
    unittest.main()
