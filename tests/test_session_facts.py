"""sessions attach-facts: the session fact attachment <part>.facts.md (vault-graph contract vault-session-facts/v1).

The command checks a facts JSON (vault-session-facts-input/1) against one vault-session/v2 part file and
writes one immutable attachment next to it. sessions git-check, sessions status and sessions capture do
not count the attachment as a session file.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.wiki.lib.frontmatter import render_page  # noqa: E402
from tests.test_wiki_tools import (  # noqa: E402
    _commit_all,
    _git,
    _init_git_vault,
    _minimal_vault,
    _run_wiki_cli,
)

SESSION_ID = "45d576f0-4bf0-56dd-8201-703456b8e6f4"
PART_REL = f"raw/sessions/claude-code/2023-05-20-{SESSION_ID}-part1.md"
FACTS_REL = f"raw/sessions/claude-code/2023-05-20-{SESSION_ID}-part1.facts.md"
NOW = datetime(2026, 11, 16, 3, 0, 0, tzinfo=timezone.utc)

PART_BODY = (
    "## 보석 감정 문의 2023-05-20T10:00:00Z · x0001\n"
    "<!-- vg:exchange id=x0001 scope=main started=2023-05-20T10:00:00Z ended=2023-05-20T10:05:00Z "
    "tools= errors=0 outcome=answered files= -->\n"
    "\n"
    "### user · 2023-05-20T10:00:00Z · t0000001\n"
    "\n"
    "물려받은 보석과 가구 감정을 알아보고 있어요.\n"
    "&lt;!-- vg:exchange id=x0009 scope=main -->\n"
    "\n"
    "## Follow-up 2023-05-21T09:00:00Z · x0002\n"
    "<!-- vg:exchange id=x0002 scope=main started=2023-05-21T09:00:00Z ended=2023-05-21T09:01:00Z "
    "tools= errors=0 outcome=answered files= -->\n"
    "\n"
    "### user · 2023-05-21T09:00:00Z · t0000002\n"
    "\n"
    "Thanks.\n"
)


def _v2_frontmatter(**overrides: object) -> dict:
    frontmatter: dict = {
        "schema": "vault-session/v2",
        "renderer": "vg-session-md/2.0",
        "source_schema": "vg-session-jsonl/1",
        "agent": "claude-code",
        "session_id": SESSION_ID,
        "project": "longmemeval",
        "cwd": "$HOME/repos/demo",
        "part": 1,
        "first_exchange_id": "x0001",
        "last_exchange_id": "x0002",
        "first_turn_uuid": "t0000001",
        "first_turn_at": "2023-05-20T10:00:00Z",
        "last_turn_uuid": "t0000002",
        "last_turn_at": "2023-05-21T09:00:00Z",
        "exchange_count": 2,
        "turn_count": 2,
        "collected_at": "2026-11-16T02:00:00Z",
        "trigger": "manual",
        "redactions": 0,
        "body_sha256": "",
    }
    frontmatter.update(overrides)
    return frontmatter


def _write_part(root: Path, relative: str = PART_REL, frontmatter: dict | None = None, body: str = PART_BODY) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    values = dict(frontmatter if frontmatter is not None else _v2_frontmatter())
    values["body_sha256"] = ""
    body_part = render_page(values, body).encode("utf-8").split(b"\n---\n", 1)[1]
    values["body_sha256"] = hashlib.sha256(body_part).hexdigest()
    path.write_bytes(render_page(values, body).encode("utf-8"))
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AttachFactsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _minimal_vault(self.root)
        self.part = _write_part(self.root)
        self.facts_path = self.root.parent / f"{self.root.name}-facts.json"
        self.attachment = self.root / FACTS_REL

    def tearDown(self):
        self.facts_path.unlink(missing_ok=True)
        self._tmp.cleanup()

    def _input(self, **overrides: object) -> dict:
        data: dict = {
            "schema": "vault-session-facts-input/1",
            "session_id": SESSION_ID,
            "source_sha256": _sha(self.part),
            "extractor": "claude",
            "model": "claude-sonnet-5-5",
            "facts": [
                {"date": "2023-05-20", "text": "사용자는 물려받은 보석과 가구 감정을 알아보고 있다.", "exchange_id": "x0001"},
                {"date": "2023-05-21", "text": "The user thanked the assistant.", "exchange_id": "x0002"},
            ],
        }
        data.update(overrides)
        return data

    def _fact(self, **overrides: str) -> list:
        fact = {"date": "2023-05-20", "text": "사용자는 보석 감정을 알아보고 있다.", "exchange_id": "x0001"}
        fact.update(overrides)
        return [fact]

    def _attach(self, data: object | None = None, session: str = PART_REL) -> tuple[int, dict]:
        if data is not None:
            raw = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            self.facts_path.write_text(raw, encoding="utf-8")
        code, output = _run_wiki_cli(
            self.root, "sessions", "attach-facts", "--session", session, "--facts", str(self.facts_path), "--json"
        )
        return code, json.loads(output.strip().splitlines()[-1])

    def _assert_rejected(self, reason: str, data: object, session: str = PART_REL) -> None:
        part_before = self.part.read_bytes()
        code, result = self._attach(data, session)
        self.assertEqual(1, code, result)
        self.assertEqual({"status": "rejected", "path": FACTS_REL, "reason": reason, "facts": 0}, result)
        self.assertFalse(self.attachment.exists())
        self.assertEqual([], [p.name for p in self.attachment.parent.iterdir() if p.name.startswith(".")])
        self.assertEqual(part_before, self.part.read_bytes())

    # Normal input and rerun

    def test_written_attachment_follows_contract_format(self):
        from tools.wiki.lib.session_capture import attach_session_facts

        result = attach_session_facts(self.root, Path(PART_REL), self._write_input(self._input()), now=NOW)

        self.assertEqual(("written", FACTS_REL, None, 2), (result.status, result.path, result.reason, result.facts))
        self.assertEqual(
            "---\n"
            "schema: vault-session-facts/v1\n"
            "agent: claude-code\n"
            f'session_id: "{SESSION_ID}"\n'
            "part: 1\n"
            f"source: {PART_REL}\n"
            f'source_sha256: "{_sha(self.part)}"\n'
            "extractor: claude\n"
            "model: claude-sonnet-5-5\n"
            'generated_at: "2026-11-16T03:00:00Z"\n'
            "fact_count: 2\n"
            "---\n"
            "\n"
            "- 2023-05-20 사용자는 물려받은 보석과 가구 감정을 알아보고 있다. (exchange x0001)\n"
            "- 2023-05-21 The user thanked the assistant. (exchange x0002)\n",
            self.attachment.read_text(encoding="utf-8"),
        )

    def _write_input(self, data: dict) -> Path:
        self.facts_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return self.facts_path

    def test_cli_written_then_rerun_exists_and_leaves_file(self):
        part_before = self.part.read_bytes()
        code, result = self._attach(self._input())
        self.assertEqual(0, code, result)
        self.assertEqual({"status": "written", "path": FACTS_REL, "reason": None, "facts": 2}, result)
        written = self.attachment.read_bytes()

        code, result = self._attach(self._input())
        self.assertEqual(0, code, result)
        self.assertEqual({"status": "exists", "path": FACTS_REL, "reason": None, "facts": 2}, result)
        self.assertEqual(written, self.attachment.read_bytes())
        self.assertEqual(part_before, self.part.read_bytes())

    def test_exists_is_checked_before_validation(self):
        self._attach(self._input())
        written = self.attachment.read_bytes()

        code, result = self._attach({"schema": "wrong"})

        self.assertEqual(0, code, result)
        self.assertEqual("exists", result["status"])
        self.assertEqual(written, self.attachment.read_bytes())

    def test_absolute_session_path_is_accepted(self):
        code, result = self._attach(self._input(), session=str(self.part))

        self.assertEqual(0, code, result)
        self.assertEqual(FACTS_REL, result["path"])
        self.assertIn(f"source: {PART_REL}\n", self.attachment.read_text(encoding="utf-8"))

    def test_text_output_without_json(self):
        self._write_input(self._input())
        code, output = _run_wiki_cli(
            self.root, "sessions", "attach-facts", "--session", PART_REL, "--facts", str(self.facts_path)
        )
        self.assertEqual(0, code, output)
        self.assertIn(f"Wrote {FACTS_REL} (2 facts).", output)

    def test_missing_arguments_are_usage_error(self):
        with self.assertRaises(SystemExit) as raised:
            _run_wiki_cli(self.root, "sessions", "attach-facts", "--session", PART_REL)
        self.assertEqual(2, raised.exception.code)

    # Contract section 4: one test per reason code

    def test_session_missing_when_part_does_not_exist(self):
        data = self._input()
        self.part.unlink()
        self.part = self.root / "raw" / "sources" / "kept.md"
        self.part.write_text("unrelated\n")
        self._assert_rejected("session_missing", data)

    def test_session_missing_when_part_is_outside_raw_sessions(self):
        other = self.root / "raw" / "sources" / self.part.name
        other.write_bytes(self.part.read_bytes())
        code, result = self._attach(self._input(), session=other.relative_to(self.root).as_posix())
        self.assertEqual(1, code, result)
        self.assertEqual("session_missing", result["reason"])
        self.assertFalse(other.with_name(other.name[:-3] + ".facts.md").exists())

    def test_session_missing_when_part_is_not_v2(self):
        from tests.test_wiki_tools import _session_frontmatter, _write_session_file

        _write_session_file(self.root, relative=PART_REL, frontmatter=_session_frontmatter(session_id=SESSION_ID))
        self._assert_rejected("session_missing", self._input(source_sha256=_sha(self.part)))

    def test_session_missing_when_part_body_was_changed(self):
        self.part.write_bytes(self.part.read_bytes() + b"tampered\n")
        self._assert_rejected("session_missing", self._input(source_sha256=_sha(self.part)))

    def test_source_mismatch(self):
        self._assert_rejected("source_mismatch", self._input(source_sha256="a" * 64))

    def test_session_id_mismatch(self):
        self._assert_rejected("session_id_mismatch", self._input(session_id="other-session"))

    def test_bad_input_wrong_schema(self):
        self._assert_rejected("bad_input", self._input(schema="vault-session-facts-input/2"))

    def test_bad_input_missing_key(self):
        data = self._input()
        del data["model"]
        self._assert_rejected("bad_input", data)

    def test_bad_input_wrong_types(self):
        self._assert_rejected("bad_input", self._input(facts="not a list"))
        self._assert_rejected("bad_input", self._input(facts=[{"date": "2023-05-20", "text": 3, "exchange_id": "x0001"}]))
        self._assert_rejected("bad_input", self._input(extractor="gpt"))
        self._assert_rejected("bad_input", self._input(model=""))
        self._assert_rejected("bad_input", "not json")
        self._assert_rejected("bad_input", "[]")

    def test_no_facts(self):
        self._assert_rejected("no_facts", self._input(facts=[]))

    def test_unknown_exchange(self):
        self._assert_rejected("unknown_exchange", self._input(facts=self._fact(exchange_id="x0003")))

    def test_unknown_exchange_ignores_escaped_meta_line_in_turn_text(self):
        self._assert_rejected("unknown_exchange", self._input(facts=self._fact(exchange_id="x0009")))

    def test_bad_text(self):
        for text in ("", "두 줄\n사실", " 앞 공백", "가" * 401):
            with self.subTest(text=text[:10]):
                self._assert_rejected("bad_text", self._input(facts=self._fact(text=text)))

    def test_text_of_400_characters_is_accepted(self):
        code, result = self._attach(self._input(facts=self._fact(text="가" * 400)))
        self.assertEqual(0, code, result)
        self.assertEqual("written", result["status"])

    def test_bad_date(self):
        for value in ("2023-02-30", "2023-5-20", "20230520", "2023-05-20T00:00:00Z"):
            with self.subTest(value=value):
                self._assert_rejected("bad_date", self._input(facts=self._fact(date=value)))

    def test_secret(self):
        for text in ("배포 키는 password=hunter2abc 이다.", "토큰 ghp_abcdefghijklmnopqrstuvwxyz0123 사용"):
            with self.subTest(text=text):
                self._assert_rejected("secret", self._input(facts=self._fact(text=text)))


class FactsNotSessionFilesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _minimal_vault(self.root)
        self.part = _write_part(self.root)
        self.attachment = self.root / FACTS_REL
        self.attachment.write_text(
            "---\nschema: vault-session-facts/v1\nfact_count: 1\n---\n\n- 2023-05-20 사실. (exchange x0001)\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_archive_check_and_status_do_not_count_attachment(self):
        from tools.wiki.lib.session_archive import list_session_files, validate_session_archive

        self.assertEqual([self.part], list_session_files(self.root))
        self.assertEqual([], validate_session_archive(self.root))
        code, output = _run_wiki_cli(self.root, "sessions", "status", "--json")
        self.assertEqual(0, code, output)
        status = json.loads(output)
        self.assertEqual(1, status["session_files"])
        self.assertEqual(0, status["archive_check_failures"])

    def test_status_uncommitted_count_skips_attachment(self):
        _init_git_vault(self.root)
        _commit_all(self.root, "vault")
        self.attachment.write_text(self.attachment.read_text() + "\n", encoding="utf-8")
        new_attachment = self.root / FACTS_REL.replace("part1", "part2")
        new_attachment.write_text("x\n", encoding="utf-8")

        code, output = _run_wiki_cli(self.root, "sessions", "status", "--json")

        self.assertEqual(0, code, output)
        self.assertEqual(0, json.loads(output)["uncommitted_session_files"])

    def test_capture_part_numbers_ignore_attachment(self):
        from tools.wiki.lib.session_capture import _part_taken, find_previous_part, highest_part

        (self.root / FACTS_REL.replace("part1", "part2")).write_text("x\n", encoding="utf-8")
        archive = self.root / "raw" / "sessions" / "claude-code"

        self.assertEqual(1, highest_part(self.root, "claude-code", SESSION_ID))
        self.assertEqual(1, find_previous_part(self.root, "claude-code", SESSION_ID).part)
        self.assertFalse(_part_taken(archive, SESSION_ID, 2))

    def test_git_check_does_not_count_attachment_but_fails_when_it_changes(self):
        _init_git_vault(self.root)
        _commit_all(self.root, "session and facts")

        code, output = _run_wiki_cli(self.root, "sessions", "git-check")
        self.assertEqual(0, code, output)
        self.assertIn("1 committed session files and 1 fact attachments unchanged", output)

        self.attachment.write_text(self.attachment.read_text() + "- 2023-05-21 더함. (exchange x0002)\n")
        _commit_all(self.root, "edit facts")
        code, output = _run_wiki_cli(self.root, "sessions", "git-check")
        self.assertEqual(1, code, output)
        self.assertIn(FACTS_REL, output)
        self.assertIn("1 of 1 committed session files and 1 fact attachments", output)

    def test_git_check_fails_when_committed_attachment_is_deleted(self):
        _init_git_vault(self.root)
        _commit_all(self.root, "session and facts")
        self.attachment.unlink()

        code, output = _run_wiki_cli(self.root, "sessions", "git-check")

        self.assertEqual(1, code, output)
        self.assertIn(f"{FACTS_REL}: committed fact attachment is missing", output)

    def test_source_registry_follows_part_status(self):
        from tools.wiki.lib.source_registry import build_source_registry

        entries = {entry.raw_path: entry for entry in build_source_registry(self.root)}
        self.assertEqual(("session-facts", "session"), (entries[FACTS_REL].source_kind, entries[FACTS_REL].ingest_status))

        self.part.write_bytes(self.part.read_bytes() + b"tampered\n")
        entries = {entry.raw_path: entry for entry in build_source_registry(self.root)}
        self.assertEqual("session-invalid", entries[FACTS_REL].ingest_status)


class AttachFactsDocTests(unittest.TestCase):
    def test_agents_and_schema_allow_attach_facts_under_raw_sessions(self):
        for relative in ("AGENTS.md", "docs/agent/OPERATING-SCHEMA.md"):
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("attach-facts", text)
                self.assertIn(".facts.md", text)


if __name__ == "__main__":
    unittest.main()
