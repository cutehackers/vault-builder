"""sessions capture --normalized and the vault-session/v2 renderer (vault-graph request S10 and S1).

Input is the vault-graph normalized session JSONL vg-session-jsonl/1 (vault-graph spec 4.2.2).
Output is one new raw/sessions/ part file in vault-session/v2 (spec 4.2.5), with the part number
and cursor rules of spec 4.2.12.
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
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
NOW = datetime(2026, 11, 6, 9, 0, 0, tzinfo=timezone.utc)
SCHEMA = "vg-session-jsonl/1"


def _session(cwd: str) -> dict:
    return {
        "schema": SCHEMA,
        "kind": "session",
        "session_id": SESSION_ID,
        "agent": "claude-code",
        "agent_version": "2.1.309",
        "adapter": "claude-code/1",
        "project": "demo",
        "cwd": cwd,
        "git_branch": "main",
        "git_commit": "abc123",
        "parent_session_id": None,
        "title": "Parser work",
        "started_at": "2026-10-30T01:00:01.000Z",
        "ended_at": "2026-10-30T01:05:00.000Z",
        "source_paths": [],
        "source_sha256": "0" * 64,
    }


def _exchange(exchange_id: str, seq: int, started: str, ended: str, *, closed: bool = True,
              scope: str = "main", subagent_type: str | None = None, tools: dict | None = None,
              files: list | None = None, errors: int = 0, outcome: str = "answered") -> dict:
    return {
        "schema": SCHEMA,
        "kind": "exchange",
        "exchange_id": exchange_id,
        "seq": seq,
        "agent_scope": scope,
        "subagent_type": subagent_type,
        "parent_tool_use_id": None,
        "started_at": started,
        "ended_at": ended,
        "closed": closed,
        "prompt_source": "human",
        "tools": tools or {},
        "files_touched": files or [],
        "commands": [],
        "errors": errors,
        "outcome": outcome,
    }


def _turn(turn_id: str, exchange_id: str, role: str, kind: str, at: str, text: str = "",
          actions: list | None = None, agent_id: str | None = None) -> dict:
    return {
        "schema": SCHEMA,
        "kind": "turn",
        "turn_id": turn_id,
        "parent_id": None,
        "exchange_id": exchange_id,
        "role": role,
        "turn_kind": kind,
        "at": at,
        "agent_id": agent_id,
        "sidechain": agent_id is not None,
        "text": text,
        "actions": actions or [],
    }


def _action(tool: str, target: str | None, ok: bool = True, error_head: str | None = None) -> dict:
    return {"tool": tool, "call_id": f"call-{tool}", "target": target, "ok": ok, "error_head": error_head}


E1 = "e1000000-0000-0000-0000-000000000001"
E2 = "e2000000-0000-0000-0000-000000000002"
E3 = "e3000000-0000-0000-0000-000000000003"
E4 = "e4000000-0000-0000-0000-000000000004"


def _lines(cwd: str, *, e4_closed: bool = False, extra: list | None = None) -> list[dict]:
    lines = [
        _session(cwd),
        _exchange(E1, 1, "2026-10-30T01:00:01.000Z", "2026-10-30T01:00:06.000Z",
                  tools={"Read": 1}, files=["src/a.py"]),
        _turn(E1, E1, "user", "human", "2026-10-30T01:00:01.000Z", "Please add the parser.\nSecond line"),
        _turn("a1000000-1", E1, "assistant", "assistant_text", "2026-10-30T01:00:03.000Z", "I will read the file."),
        _turn("a1000000-2", E1, "assistant", "tool_only", "2026-10-30T01:00:04.000Z",
              actions=[_action("Read", "src/a.py")]),
        _turn("a1000000-3", E1, "assistant", "assistant_text", "2026-10-30T01:00:06.000Z", "Done.\n## not a heading"),
        _exchange(E2, 2, "2026-10-30T01:00:07.000Z", "2026-10-30T01:00:09.000Z",
                  scope="subagent:abc123", subagent_type="Explore"),
        _turn("s2000000-1", E2, "user", "human", "2026-10-30T01:00:07.000Z", "Find parsers.", agent_id="abc123"),
        _turn("s2000000-2", E2, "assistant", "assistant_text", "2026-10-30T01:00:09.000Z", "Found two.",
              agent_id="abc123"),
        _exchange(E3, 3, "2026-10-30T01:01:00.000Z", "2026-10-30T01:01:05.000Z",
                  tools={"Bash": 1}, errors=1, outcome="tool_error_unresolved"),
        _turn(E3, E3, "user", "human", "2026-10-30T01:01:00.000Z", "Run tests with api_key=abcdef123456 please"),
        _turn("a3000000-1", E3, "assistant", "assistant_text", "2026-10-30T01:01:05.000Z", "Tests failed.",
              actions=[_action("Bash", "pytest -q", ok=False, error_head="ImportError: no module x")]),
        _exchange(E4, 4, "2026-10-30T01:02:00.000Z", "2026-10-30T01:02:05.000Z", closed=e4_closed),
        _turn(E4, E4, "user", "human", "2026-10-30T01:02:00.000Z", "Open question"),
        _turn("a4000000-1", E4, "assistant", "assistant_text", "2026-10-30T01:02:05.000Z", "Working on it."),
    ]
    if extra:
        lines.extend(extra)
    return lines


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True, capture_output=True)
    return path


class NormalizedCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.base = Path(tmpdir.name).resolve()
        self.home = self.base / "home"
        self.config = self.base / "config"
        self.repo = _git_repo(self.home / "repos" / "demo")
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.config)})
        patcher = unittest.mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = self._vault("app")
        self._write_map({"app": self.app})
        self.normalized = self.base / "export" / f"{SESSION_ID}.jsonl"
        self._write_normalized(_lines(str(self.repo)))

    # Fixture helpers

    def _vault(self, name: str) -> Path:
        path = self.base / "vaults" / name
        (path / "wiki").mkdir(parents=True)
        manifest = path / ".vault-builder" / "manifest.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"contract_version": 3, "schema_version": 3}), encoding="utf-8")
        return path

    def _write_map(self, vaults: dict[str, Path]) -> None:
        path = self.config / "vault-builder" / "vault-map.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 2,
            "vaults": {name: {"path": str(vault), "role": "project"} for name, vault in vaults.items()},
            "projects": {"demo": {"root": str(self.repo), "vaults": list(vaults)}},
        }
        path.write_text(json.dumps(data), encoding="utf-8")

    def _write_normalized(self, lines: list[dict], path: Path | None = None) -> Path:
        path = path or self.normalized
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
        return path

    def _capture(self, *args: str, stdin: str = "", now: datetime = NOW) -> tuple[int, str, str]:
        from tools.wiki import cli
        from tools.wiki.lib import session_capture

        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                unittest.mock.patch.object(session_capture, "_utc_now", lambda: now), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["sessions", "capture", *args])
            except SystemExit as exc:
                code = int(exc.code)
        return code, out.getvalue(), err.getvalue()

    def _normalized(self, *args: str, stdin: str = "", now: datetime = NOW) -> tuple[int, dict, str]:
        code, out, err = self._capture("--normalized", str(self.normalized), "--json", *args, stdin=stdin, now=now)
        return code, json.loads(out) if out.strip() else {}, err

    def _files(self, vault: Path | None = None) -> list[Path]:
        archive = (vault or self.app) / "raw" / "sessions"
        return sorted(path for path in archive.rglob("*") if path.is_file()) if archive.is_dir() else []

    def _split(self, path: Path) -> tuple[dict, str]:
        from tools.wiki.lib.frontmatter import _parse_frontmatter
        from tools.wiki.lib.session_archive import split_session_text

        raw, body = split_session_text(path.read_bytes().decode("utf-8"))
        return _parse_frontmatter(raw)[0], body

    def _write_v1_part(self, part: int, last_turn_uuid: str, last_turn_at: str) -> Path:
        from tools.wiki.lib.session_capture import SessionMeta, Turn, parse_turn_at, render_session_file

        meta = SessionMeta(session_id=SESSION_ID, project="demo", cwd="$HOME/repos/demo", part=part,
                           collected_at="2026-10-30T02:00:00Z", trigger="PreCompact")
        turn = Turn(uuid=last_turn_uuid, at=parse_turn_at(last_turn_at), role="assistant", agent_id=None,
                    text="v1 text", tools=())
        path = self.app / "raw" / "sessions" / "claude-code" / f"2026-10-30-{SESSION_ID}-part{part}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render_session_file(meta, [turn]).encode("utf-8"))
        return path

    # Writing a v2 part

    def test_first_capture_writes_part1_in_v2_with_closed_exchanges_only(self):
        from tools.wiki.lib.session_archive import SESSION_V2_FRONTMATTER_FIELDS, check_session_file

        code, result, err = self._normalized()

        self.assertEqual(0, code, err)
        files = self._files()
        self.assertEqual([f"2026-10-30-{SESSION_ID}-part1.md"], [path.name for path in files])
        self.assertEqual("written", result["status"])
        self.assertEqual("vault-session/v2", result["schema"])
        self.assertEqual(1, result["part"])
        self.assertEqual(3, result["exchange_count"])
        self.assertEqual(f"raw/sessions/claude-code/2026-10-30-{SESSION_ID}-part1.md", result["path"])
        frontmatter, body = self._split(files[0])
        raw_keys = [line.split(":", 1)[0] for line in
                    files[0].read_text(encoding="utf-8").split("\n---\n", 1)[0].splitlines()[1:]]
        self.assertEqual(list(SESSION_V2_FRONTMATTER_FIELDS), raw_keys)
        self.assertEqual("vault-session/v2", frontmatter["schema"])
        self.assertEqual("vg-session-md/2.0", frontmatter["renderer"])
        self.assertEqual("vg-session-jsonl/1", frontmatter["source_schema"])
        self.assertEqual("claude-code", frontmatter["agent"])
        self.assertEqual(SESSION_ID, frontmatter["session_id"])
        self.assertEqual("demo", frontmatter["project"])
        self.assertEqual("$HOME/repos/demo", frontmatter["cwd"])
        self.assertEqual(1, frontmatter["part"])
        self.assertEqual(E1, frontmatter["first_exchange_id"])
        self.assertEqual(E3, frontmatter["last_exchange_id"])
        self.assertEqual(E1, frontmatter["first_turn_uuid"])
        self.assertEqual("a3000000-1", frontmatter["last_turn_uuid"])
        self.assertEqual("2026-10-30T01:01:05.000Z", frontmatter["last_turn_at"])
        self.assertEqual(3, frontmatter["exchange_count"])
        self.assertEqual(8, frontmatter["turn_count"])
        self.assertEqual("2026-11-06T09:00:00Z", frontmatter["collected_at"])
        self.assertEqual("manual", frontmatter["trigger"])
        self.assertEqual(1, frontmatter["redactions"])
        self.assertTrue(check_session_file(self.app, files[0]).ok, check_session_file(self.app, files[0]).issues)
        self.assertNotIn("Open question", body)
        self.assertNotIn(E4, body)
        self.assertNotIn("abcdef123456", body)
        self.assertNotIn(str(self.home), files[0].read_text(encoding="utf-8"))

    def test_body_layout(self):
        self._normalized()
        _, body = self._split(self._files()[0])

        expected = (
            "\n"
            f"## Please add the parser. Second line 2026-10-30T01:00:01.000Z · e1000000\n"
            f"<!-- vg:exchange id={E1} scope=main started=2026-10-30T01:00:01.000Z "
            "ended=2026-10-30T01:00:06.000Z tools=Read:1 errors=0 outcome=answered files=src/a.py -->\n"
            "\n"
            "### user · 2026-10-30T01:00:01.000Z · e1000000\n"
            "\n"
            "Please add the parser.\nSecond line\n"
            "\n"
            "### assistant · 2026-10-30T01:00:03.000Z · a1000000\n"
            "\n"
            "I will read the file.\n"
            "\n"
            "- Read `src/a.py`\n"
            "\n"
            "### assistant · 2026-10-30T01:00:06.000Z · a1000000\n"
            "\n"
            "Done.\n\\## not a heading\n"
            "\n"
            "## Run tests with api_key=[REDACTED:secret] please 2026-10-30T01:01:00.000Z · e3000000\n"
            f"<!-- vg:exchange id={E3} scope=main started=2026-10-30T01:01:00.000Z "
            "ended=2026-10-30T01:01:05.000Z tools=Bash:1 errors=1 outcome=tool_error_unresolved files= -->\n"
            "\n"
            "### user · 2026-10-30T01:01:00.000Z · e3000000\n"
            "\n"
            "Run tests with api_key=[REDACTED:secret] please\n"
            "\n"
            "### assistant · 2026-10-30T01:01:05.000Z · a3000000\n"
            "\n"
            "Tests failed.\n"
            "\n"
            "- Bash `pytest -q` → 실패: ImportError: no module x\n"
            "\n"
            "## Subagent abc123 · Explore · 2026-10-30T01:00:07.000Z · e2000000\n"
            f"<!-- vg:exchange id={E2} scope=subagent:abc123 started=2026-10-30T01:00:07.000Z "
            "ended=2026-10-30T01:00:09.000Z tools= errors=0 outcome=answered files= -->\n"
            "\n"
            "### user · 2026-10-30T01:00:07.000Z · s2000000\n"
            "\n"
            "Find parsers.\n"
            "\n"
            "### assistant · 2026-10-30T01:00:09.000Z · s2000000\n"
            "\n"
            "Found two.\n"
        )
        self.assertEqual(expected, body)

    def test_same_input_and_time_render_the_same_bytes(self):
        from tools.wiki.lib.session_archive import session_body_sha256, split_session_text

        other = self._vault("other")
        self._normalized()
        first = self._files()[0].read_bytes()
        self._write_map({"other": other})
        self._normalized()
        second = self._files(other)[0].read_bytes()
        self.assertEqual(first, second)

        third_vault = self._vault("third")
        self._write_map({"third": third_vault})
        self._normalized(now=datetime(2027, 1, 1, tzinfo=timezone.utc))
        third = self._files(third_vault)[0].read_bytes().decode("utf-8")
        self.assertEqual(split_session_text(first.decode("utf-8"))[1], split_session_text(third)[1])
        self.assertIn(session_body_sha256(split_session_text(third)[1]), third)

    def test_no_new_exchanges_writes_nothing(self):
        self._normalized()
        code, result, err = self._normalized()
        self.assertEqual(0, code, err)
        self.assertEqual("no-new-turns", result["status"])
        self.assertEqual(1, len(self._files()))

    def test_second_capture_continues_after_last_exchange(self):
        self._normalized()
        part1 = self._files()[0].read_bytes()
        self._write_normalized(_lines(str(self.repo), e4_closed=True))

        code, result, err = self._normalized()

        self.assertEqual(0, code, err)
        files = self._files()
        self.assertEqual(2, len(files))
        self.assertEqual(part1, files[0].read_bytes())
        frontmatter, body = self._split(files[1])
        self.assertEqual(2, frontmatter["part"])
        self.assertEqual(E4, frontmatter["first_exchange_id"])
        self.assertEqual(E4, frontmatter["last_exchange_id"])
        self.assertEqual(1, frontmatter["exchange_count"])
        self.assertIn("Open question", body)
        self.assertNotIn("continued=1", body)
        self.assertNotIn("Please add the parser", body)

    def test_open_exchange_in_the_middle_stops_the_part(self):
        lines = _lines(str(self.repo), e4_closed=True)
        lines[6]["closed"] = False  # E2, the subagent exchange, is still open.
        self._write_normalized(lines)

        self._normalized()

        frontmatter, body = self._split(self._files()[0])
        self.assertEqual(E1, frontmatter["last_exchange_id"])
        self.assertNotIn("Run tests", body)

    def test_v1_part_then_v2_continues_after_last_turn_uuid_with_continued_exchange(self):
        v1 = self._write_v1_part(1, "a1000000-1", "2026-10-30T01:00:03.000Z")
        v1_bytes = v1.read_bytes()
        self._write_v1_part(2, "a1000000-1", "2026-10-30T01:00:03.000Z")

        code, result, err = self._normalized()

        self.assertEqual(0, code, err)
        files = self._files()
        self.assertEqual(3, len(files))
        self.assertEqual(v1_bytes, v1.read_bytes())
        self.assertEqual(3, result["part"])
        frontmatter, body = self._split(files[2])
        self.assertEqual("vault-session/v2", frontmatter["schema"])
        self.assertEqual(3, frontmatter["part"])
        self.assertEqual("a1000000-2", frontmatter["first_turn_uuid"])
        self.assertEqual(E1, frontmatter["first_exchange_id"])
        self.assertIn(
            "## (이전 파트에서 이어짐) 2026-10-30T01:00:04.000Z · e1000000\n"
            f"<!-- vg:exchange id={E1} scope=main started=2026-10-30T01:00:04.000Z "
            "ended=2026-10-30T01:00:06.000Z tools=Read:1 errors=0 outcome=answered files=src/a.py continued=1 -->\n"
            "\n### assistant · 2026-10-30T01:00:04.000Z · a1000000\n\n- Read `src/a.py`\n"
            "\n### assistant · 2026-10-30T01:00:06.000Z · a1000000\n",
            body,
        )
        self.assertNotIn("I will read the file.", body)
        self.assertNotIn("Please add the parser", body)
        self.assertEqual(1, body.count("continued=1"))

    def test_v1_cursor_missing_from_input_keeps_later_turns_with_warning(self):
        self._write_v1_part(1, "not-in-input", "2026-10-30T01:00:05.000Z")

        code, result, err = self._normalized()

        self.assertEqual(0, code, err)
        self.assertTrue(any("not-in-input" in warning for warning in result["warnings"]))
        frontmatter, body = self._split(self._files()[1])
        self.assertEqual("a1000000-3", frontmatter["first_turn_uuid"])
        self.assertIn("continued=1", body)

    def test_rerender_ignores_cursor_and_keeps_existing_files(self):
        v1 = self._write_v1_part(1, "a3000000-1", "2026-10-30T01:01:05.000Z")
        v1_bytes = v1.read_bytes()

        code, result, err = self._normalized("--rerender")

        self.assertEqual(0, code, err)
        files = self._files()
        self.assertEqual(2, len(files))
        self.assertEqual(v1_bytes, v1.read_bytes())
        frontmatter, body = self._split(files[1])
        self.assertEqual(2, frontmatter["part"])
        self.assertEqual(E1, frontmatter["first_exchange_id"])
        self.assertEqual(E3, frontmatter["last_exchange_id"])
        self.assertNotIn("continued=1", body)

        code, result, err = self._normalized()
        self.assertEqual("no-new-turns", result["status"], err)

    def test_hook_json_on_stdin_gives_trigger_and_exit_zero(self):
        hook = json.dumps({"session_id": SESSION_ID, "cwd": str(self.repo), "hook_event_name": "SessionEnd",
                           "transcript_path": "/nowhere.jsonl"})
        code, result, err = self._normalized(stdin=hook)
        self.assertEqual(0, code, err)
        self.assertEqual("SessionEnd", self._split(self._files()[0])[0]["trigger"])

        self.normalized = self.base / "missing.jsonl"
        code, result, err = self._normalized(stdin=hook)
        self.assertEqual(0, code)
        self.assertEqual("skipped", result["status"])

    def test_bad_input_writes_nothing_and_exits_one(self):
        cases = {
            "not-json": "not json\n",
            "wrong-schema": json.dumps({**_session(str(self.repo)), "schema": "vg-session-jsonl/9"}) + "\n",
            "no-session-first": json.dumps(_lines(str(self.repo))[1]) + "\n",
            "turn-wrong-exchange": "".join(
                json.dumps(line) + "\n"
                for line in [_session(str(self.repo)), _exchange(E1, 1, "2026-10-30T01:00:01.000Z", "x"),
                             _turn("t", E2, "user", "human", "2026-10-30T01:00:01.000Z", "hi")]
            ),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.normalized.write_text(text, encoding="utf-8")
                code, result, err = self._normalized()
                self.assertEqual(1, code, name)
                self.assertEqual("skipped", result["status"])
                self.assertEqual([], self._files())

    def test_unknown_kind_lines_are_skipped_with_warning(self):
        self._write_normalized(_lines(str(self.repo), extra=[{"schema": SCHEMA, "kind": "future"}]))
        code, result, err = self._normalized()
        self.assertEqual(0, code, err)
        self.assertTrue(any("future" in warning for warning in result["warnings"]))

    def test_cwd_from_session_line_picks_the_vault(self):
        code, result, err = self._normalized()
        self.assertEqual("app", result["vault"], err)

    def test_options_exit_two(self):
        for args in (["--rerender"], ["--normalized", "x.jsonl", "--transcript", "t.jsonl"], ["--bogus"]):
            with self.subTest(args):
                code, _, _ = self._capture(*args)
                self.assertEqual(2, code)

    def test_v1_capture_after_v2_part_continues_after_its_last_turn(self):
        from tools.wiki.lib.session_capture import PartCursor, find_previous_part

        self._normalized()
        cursor = find_previous_part(self.app, "claude-code", SESSION_ID)
        self.assertEqual(PartCursor("a3000000-1", "2026-10-30T01:01:05.000Z", 1, E3), cursor)


class SessionFormatDocTests(unittest.TestCase):
    def test_operating_schema_documents_v2(self):
        text = (ROOT / "docs" / "agent" / "OPERATING-SCHEMA.md").read_text(encoding="utf-8")
        for needle in ("vault-session/v2", "vg-session-md/2.0", "<!-- vg:exchange", "continued=1", "--rerender",
                       "last_exchange_id"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
