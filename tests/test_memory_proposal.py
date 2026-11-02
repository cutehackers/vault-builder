"""Contract 4: the `memory-proposal` review type, `review propose`, and publish on accept.

Every test runs on a copy of this repository's lint-clean wiki in a temporary folder,
so proposing and publishing never touch the working tree.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shutil
import tempfile
import unittest
import unittest.mock
from datetime import date
from pathlib import Path

from tools.wiki import cli
from tools.wiki.lib.frontmatter import parse_page
from tools.wiki.lib.lint import lint_repo
from tools.wiki.lib.memory_proposal import REVIEW_RESULT_FORMAT, accept, propose
from tools.wiki.lib.publish import publish_draft
from tools.wiki.lib.review import validate_review_queue

REPO = Path(__file__).resolve().parents[1]
PROPOSE_FIELDS = {"format", "status", "review_id", "review_path", "draft_path", "target_path", "messages"}
REVIEW_ID_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-memory-[0-9a-f]{8}$")


def _fixture_vault(root: Path) -> None:
    shutil.copytree(REPO / "wiki", root / "wiki")
    shutil.copytree(REPO / "raw", root / "raw")
    shutil.copytree(REPO / "tools" / "wiki", root / "tools" / "wiki", ignore=shutil.ignore_patterns("__pycache__"))
    for folder in ("drafts", "reports", "review"):
        (root / "scratch" / folder).mkdir(parents=True, exist_ok=True)


def _memory_draft(slug: str, summary: str = "에이전트가 추론한 기억이다.") -> dict:
    return {
        "version": 1,
        "operation": "upsert-page",
        "path": f"wiki/concepts/{slug}.md",
        "frontmatter": {
            "title": "Memory Proposal Test",
            "type": "concept",
            "status": "active",
            "created": "2026-11-02",
            "updated": "2026-11-02",
            "owner": "agent",
            "summary": summary,
            "source_count": 0,
            "tags": ["test"],
            "related": ["index"],
            "confidence": "high",
            "quality": {
                "provenance": "none",
                "links": "unchecked",
                "contradictions": "none",
                "review_required": False,
            },
        },
        "body": "# Memory Proposal Test\n\nAn inferred memory waiting for human review.\n",
        "index": {
            "section": "Concepts",
            "target": f"concepts/{slug}",
            "summary": summary,
        },
        "log": {
            "event_type": "repair",
            "title": "Memory Proposal Test",
            "items": [f"Added [[concepts/{slug}]]."],
        },
    }


def _tree_bytes(folder: Path) -> dict[str, bytes]:
    return {path.relative_to(folder).as_posix(): path.read_bytes() for path in sorted(folder.rglob("*")) if path.is_file()}


class MemoryProposalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _fixture_vault(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_draft(self, name: str, draft: dict) -> Path:
        path = self.root / "scratch" / "drafts" / f"{name}.json"
        path.write_text(json.dumps(draft, indent=2, ensure_ascii=False))
        return path

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with unittest.mock.patch.object(cli, "ROOT", self.root):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def propose_json(self, draft_path: Path, summary: str = "추론한 기억 제안") -> tuple[int, dict]:
        code, stdout, _stderr = self.run_cli("review", "propose", "--draft", str(draft_path), "--summary", summary, "--json")
        payload = json.loads(stdout)
        self.assertEqual(PROPOSE_FIELDS, set(payload))
        self.assertEqual("vault-review-result/v1", payload["format"])
        return code, payload

    def resolve_json(self, review_path: str, status: str, resolution: str = "사람이 확인했다.") -> tuple[int, dict]:
        code, stdout, _stderr = self.run_cli(
            "review", "resolve", review_path, "--status", status, "--resolution", resolution, "--json"
        )
        payload = json.loads(stdout)
        self.assertEqual(REVIEW_RESULT_FORMAT, payload["format"])
        return code, payload

    def review_frontmatter(self, review_path: str) -> dict:
        page = parse_page(self.root / review_path)
        assert page.frontmatter is not None
        return dict(page.frontmatter)


class ProposeTests(MemoryProposalTestCase):
    def test_review_id_is_date_memory_and_draft_hash_so_korean_proposals_differ(self):
        first = self.write_draft("first", _memory_draft("memory-proposal-a", summary="첫 번째 추론 기억이다."))
        second = self.write_draft("second", _memory_draft("memory-proposal-a", summary="두 번째 추론 기억이다."))

        result_a = propose(self.root, first, "한국어 요약 제안", [], [])
        result_b = propose(self.root, second, "한국어 요약 제안", [], [])

        self.assertEqual("proposed", result_a.status, result_a.messages)
        self.assertEqual("proposed", result_b.status, result_b.messages)
        self.assertRegex(result_a.review_id, REVIEW_ID_PATTERN)
        self.assertNotEqual(result_a.review_id, result_b.review_id)
        expected = f"{date.today().isoformat()}-memory-{hashlib.sha256(first.read_bytes()).hexdigest()[:8]}"
        self.assertEqual(expected, result_a.review_id)

    def test_propose_writes_pending_item_and_draft_copy_without_touching_wiki(self):
        draft_path = self.write_draft("new", _memory_draft("memory-proposal-new"))
        wiki_before = _tree_bytes(self.root / "wiki")

        code, payload = self.propose_json(draft_path)

        self.assertEqual(0, code, payload)
        self.assertEqual("proposed", payload["status"])
        review_id = payload["review_id"]
        self.assertEqual(f"scratch/review/{review_id}.md", payload["review_path"])
        self.assertEqual(f"scratch/review/{review_id}.draft.json", payload["draft_path"])
        self.assertEqual("wiki/concepts/memory-proposal-new.md", payload["target_path"])
        self.assertEqual(draft_path.read_bytes(), (self.root / payload["draft_path"]).read_bytes())

        frontmatter = self.review_frontmatter(payload["review_path"])
        self.assertEqual("memory-proposal", frontmatter["type"])
        self.assertEqual("pending", frontmatter["status"])
        self.assertEqual(review_id, frontmatter["review_id"])
        self.assertEqual(payload["draft_path"], frontmatter["draft"])
        self.assertEqual(payload["target_path"], frontmatter["target_path"])
        self.assertEqual("", frontmatter["target_hash"])

        self.assertEqual(wiki_before, _tree_bytes(self.root / "wiki"))
        self.assertTrue(lint_repo(self.root).ok)

    def test_propose_records_sha256_of_an_existing_target(self):
        target = self.root / "wiki" / "concepts" / "memory-proposal-existing.md"
        self.assertEqual("published", publish_draft(self.root, self.write_draft("seed", _memory_draft("memory-proposal-existing"))).status)
        update = self.write_draft("update", _memory_draft("memory-proposal-existing", summary="고친 기억이다."))

        result = propose(self.root, update, "기존 기억 수정 제안", [], [])

        self.assertEqual("proposed", result.status, result.messages)
        frontmatter = self.review_frontmatter(result.review_path)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), frontmatter["target_hash"])

    def test_invalid_draft_writes_no_files_and_exits_1(self):
        draft = _memory_draft("memory-proposal-invalid")
        del draft["frontmatter"]["title"]
        draft_path = self.write_draft("invalid", draft)
        scratch_before = _tree_bytes(self.root / "scratch")
        wiki_before = _tree_bytes(self.root / "wiki")

        code, payload = self.propose_json(draft_path)

        self.assertEqual(1, code, payload)
        self.assertEqual("rejected", payload["status"])
        self.assertIsNone(payload["review_id"])
        self.assertIsNone(payload["review_path"])
        self.assertIsNone(payload["draft_path"])
        self.assertTrue(payload["messages"])
        self.assertEqual(scratch_before, _tree_bytes(self.root / "scratch"))
        self.assertEqual(wiki_before, _tree_bytes(self.root / "wiki"))

    def test_missing_related_page_writes_no_files(self):
        draft_path = self.write_draft("related", _memory_draft("memory-proposal-related"))
        scratch_before = _tree_bytes(self.root / "scratch")

        result = propose(self.root, draft_path, "관련 페이지 없음", [], ["wiki/concepts/does-not-exist.md"])

        self.assertEqual("rejected", result.status)
        self.assertEqual(scratch_before, _tree_bytes(self.root / "scratch"))

    def test_propose_text_output_without_json(self):
        draft_path = self.write_draft("text", _memory_draft("memory-proposal-text"))

        code, stdout, _stderr = self.run_cli("review", "propose", "--draft", str(draft_path), "--summary", "텍스트 출력")

        self.assertEqual(0, code)
        self.assertIn("Review item created: scratch/review/", stdout)


class AcceptTests(MemoryProposalTestCase):
    def propose_new(self, slug: str) -> dict:
        draft_path = self.write_draft(slug, _memory_draft(slug))
        code, payload = self.propose_json(draft_path)
        self.assertEqual(0, code, payload)
        return payload

    def test_accept_publishes_draft_and_marks_item_accepted(self):
        proposal = self.propose_new("memory-proposal-accept")

        code, payload = self.resolve_json(proposal["review_path"], "accepted")

        self.assertEqual(0, code, payload)
        self.assertEqual("resolved", payload["status"])
        self.assertEqual("accepted", payload["review_status"])
        self.assertIn("wiki/concepts/memory-proposal-accept.md", payload["written_paths"])
        self.assertTrue((self.root / "wiki" / "concepts" / "memory-proposal-accept.md").exists())
        frontmatter = self.review_frontmatter(proposal["review_path"])
        self.assertEqual("accepted", frontmatter["status"])
        self.assertIn("wiki/concepts/memory-proposal-accept.md", frontmatter["resolution"])
        self.assertTrue(lint_repo(self.root).ok)

    def test_accept_text_output_without_json(self):
        proposal = self.propose_new("memory-proposal-accept-text")

        code, stdout, _stderr = self.run_cli(
            "review", "resolve", proposal["review_path"], "--status", "accepted", "--resolution", "확인"
        )

        self.assertEqual(0, code, stdout)
        self.assertIn("Review item resolved", stdout)

    def test_accept_refuses_when_target_changed_after_proposal(self):
        proposal = self.propose_new("memory-proposal-changed")
        other = _memory_draft("memory-proposal-changed", summary="사람이 먼저 쓴 내용이다.")
        self.assertEqual("published", publish_draft(self.root, self.write_draft("other", other)).status)
        target = self.root / "wiki" / "concepts" / "memory-proposal-changed.md"
        target_before = target.read_bytes()

        code, payload = self.resolve_json(proposal["review_path"], "accepted")

        self.assertEqual(1, code, payload)
        self.assertEqual("rejected", payload["status"])
        self.assertEqual("target-changed", payload["reason"])
        self.assertEqual("pending", payload["review_status"])
        self.assertEqual("pending", self.review_frontmatter(proposal["review_path"])["status"])
        self.assertEqual(target_before, target.read_bytes())

    def test_publish_rejection_keeps_item_pending(self):
        proposal = self.propose_new("memory-proposal-dirty")
        (self.root / "wiki" / "concepts" / "broken.md").write_text("---\ntitle: \"Broken\"\n---\n# Broken\n")

        code, payload = self.resolve_json(proposal["review_path"], "accepted")

        self.assertEqual(1, code, payload)
        self.assertEqual("rejected", payload["status"])
        self.assertEqual("lint-dirty-before", payload["reason"])
        self.assertEqual("pending", payload["review_status"])
        self.assertEqual("pending", self.review_frontmatter(proposal["review_path"])["status"])
        self.assertFalse((self.root / "wiki" / "concepts" / "memory-proposal-dirty.md").exists())

    def test_rejected_status_changes_only_the_item_and_keeps_draft_copy(self):
        proposal = self.propose_new("memory-proposal-rejected")
        wiki_before = _tree_bytes(self.root / "wiki")

        code, payload = self.resolve_json(proposal["review_path"], "rejected", "근거가 부족하다.")

        self.assertEqual(0, code, payload)
        self.assertEqual("resolved", payload["status"])
        self.assertEqual("rejected", payload["review_status"])
        self.assertEqual("rejected", self.review_frontmatter(proposal["review_path"])["status"])
        self.assertTrue((self.root / proposal["draft_path"]).exists())
        self.assertFalse((self.root / proposal["target_path"]).exists())
        wiki_after = _tree_bytes(self.root / "wiki")
        # Resolving any review item appends one line to wiki/log.md; no other wiki file changes.
        wiki_before.pop("log.md")
        wiki_after.pop("log.md")
        self.assertEqual(wiki_before, wiki_after)

    def test_accept_function_refuses_non_pending_item(self):
        proposal = self.propose_new("memory-proposal-twice")
        self.assertEqual("resolved", accept(self.root, Path(proposal["review_path"]), "first").status)

        second = accept(self.root, Path(proposal["review_path"]), "second")

        self.assertEqual("rejected", second.status)
        self.assertEqual("accepted", second.review_status)


class MemoryProposalLintTests(MemoryProposalTestCase):
    def write_item(self, name: str, draft: str) -> None:
        (self.root / "scratch" / "review" / f"{name}.md").write_text(
            "\n".join(
                [
                    "---",
                    f"review_id: {name}",
                    "type: memory-proposal",
                    "status: pending",
                    "created: 2026-11-02",
                    "updated: 2026-11-02",
                    "summary: lint test",
                    "related_pages: []",
                    "evidence: []",
                    'resolution: ""',
                    f'draft: "{draft}"',
                    'target_path: "wiki/concepts/x.md"',
                    'target_hash: ""',
                    "---",
                    "# lint test",
                    "",
                ]
            )
        )

    def codes(self) -> list[str]:
        return [issue.code for issue in validate_review_queue(self.root)]

    def test_draft_outside_scratch_review_is_an_error(self):
        (self.root / "scratch" / "drafts" / "outside.json").write_text("{}")
        self.write_item("2026-11-02-memory-outside", "scratch/drafts/outside.json")
        self.assertIn("invalid-review-draft", self.codes())

    def test_missing_draft_is_an_error(self):
        self.write_item("2026-11-02-memory-missing", "scratch/review/missing.draft.json")
        self.assertIn("missing-review-draft", self.codes())

    def test_empty_draft_field_is_an_error(self):
        self.write_item("2026-11-02-memory-empty", "")
        self.assertIn("missing-review-draft", self.codes())

    def test_draft_under_scratch_review_passes(self):
        (self.root / "scratch" / "review" / "ok.draft.json").write_text("{}")
        self.write_item("2026-11-02-memory-ok", "scratch/review/ok.draft.json")
        self.assertEqual([], self.codes())


if __name__ == "__main__":
    unittest.main()
