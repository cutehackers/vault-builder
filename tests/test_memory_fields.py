"""Contract 2: optional memory frontmatter fields and the page status `retired`.

`validate_memory_fields` checks the five lint rules. The Vault tools that list statuses
(search, maps, metrics, merge scan) must treat `retired` like `deprecated`: not current.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tools.wiki.lib.frontmatter import Page, parse_page, render_page
from tools.wiki.lib.lint import lint_repo
from tools.wiki.lib.memory_fields import (
    ALLOWED_MEMORY_KINDS,
    ALLOWED_MEMORY_SCHEMAS,
    validate_memory_fields,
)
from tools.wiki.lib.schema import ALLOWED_STATUS, NOT_CURRENT_STATUSES, validate_schema

REPO = Path(__file__).resolve().parents[1]
MEMORY_SCHEMA = "vault-memory/v1"


def _frontmatter(title: str = "Memory Page", page_type: str = "concept", **extra: object) -> dict:
    frontmatter: dict = {
        "title": title,
        "type": page_type,
        "status": "active",
        "created": "2026-11-02",
        "updated": "2026-11-02",
        "owner": "agent",
        "summary": f"{title} summary.",
        "source_count": 0,
        "tags": ["memory"],
        "related": ["index"],
        "confidence": "high",
        "quality": {
            "provenance": "none",
            "links": "unchecked",
            "contradictions": "none",
            "review_required": False,
        },
    }
    frontmatter.update(extra)
    return frontmatter


def _page(**extra: object) -> Page:
    return Page(path=Path("wiki/concepts/memory.md"), frontmatter=_frontmatter(**extra), body="# Memory\n")


def _codes(page: Page) -> list[str]:
    return [issue.code for issue in validate_memory_fields(page)]


def _round_trip(tmp: Path, frontmatter: dict) -> Page:
    """Write the page through the real renderer and parser, as lint reads it."""
    path = tmp / "page.md"
    path.write_text(render_page(frontmatter, "# Page\n"))
    return parse_page(path)


class MemoryFieldRulesTest(unittest.TestCase):
    def test_constants_match_contract(self):
        self.assertEqual(ALLOWED_MEMORY_SCHEMAS, frozenset({MEMORY_SCHEMA}))
        self.assertEqual(ALLOWED_MEMORY_KINDS, frozenset({"rule", "correction", "decision", "preference"}))
        self.assertIn("retired", ALLOWED_STATUS)
        self.assertEqual(NOT_CURRENT_STATUSES, frozenset({"deprecated", "retired"}))

    def test_page_without_memory_fields_has_no_issues(self):
        self.assertEqual(_codes(_page()), [])

    def test_page_without_frontmatter_has_no_issues(self):
        self.assertEqual(validate_memory_fields(Page(path=Path("x.md"), frontmatter=None, body="")), [])

    def test_valid_memory_page_has_no_issues(self):
        for kind in sorted(ALLOWED_MEMORY_KINDS):
            with self.subTest(kind=kind):
                self.assertEqual(_codes(_page(memory_schema=MEMORY_SCHEMA, pin=True, memory_kind=kind)), [])
        self.assertEqual(_codes(_page(memory_schema=MEMORY_SCHEMA, pin=False)), [])
        self.assertEqual(_codes(_page(memory_schema=MEMORY_SCHEMA)), [])

    # Rule 1
    def test_pin_or_kind_without_schema_is_error(self):
        self.assertEqual(_codes(_page(pin=True)), ["memory-field-without-schema"])
        self.assertEqual(_codes(_page(memory_kind="rule")), ["memory-field-without-schema"])

    # Rule 2
    def test_unknown_schema_is_error(self):
        self.assertEqual(_codes(_page(memory_schema="vault-memory/v2")), ["invalid-memory-schema"])
        self.assertEqual(_codes(_page(memory_schema=1)), ["invalid-memory-schema"])

    # Rule 3
    def test_pin_must_be_boolean(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            page = _round_trip(Path(tmpdir), _frontmatter(memory_schema=MEMORY_SCHEMA, pin="yes"))
        self.assertEqual(page.frontmatter["pin"], "yes")
        self.assertEqual(_codes(page), ["invalid-pin"])

    def test_quoted_true_pin_is_not_boolean(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "page.md"
            path.write_text(render_page(_frontmatter(memory_schema=MEMORY_SCHEMA), "# Page\n").replace(
                "memory_schema:", 'pin: "true"\nmemory_schema:'
            ))
            page = parse_page(path)
        self.assertEqual(_codes(page), ["invalid-pin"])

    def test_unknown_memory_kind_is_error(self):
        self.assertEqual(_codes(_page(memory_schema=MEMORY_SCHEMA, memory_kind="fact")), ["invalid-memory-kind"])

    # Rule 4
    def test_retired_needs_reason_and_date(self):
        self.assertEqual(
            _codes(_page(status="retired", retired_at="2026-11-02")),
            ["retired-without-reason"],
        )
        self.assertEqual(
            _codes(_page(status="retired", retired_reason="No longer true.")),
            ["retired-without-date"],
        )
        self.assertEqual(
            sorted(_codes(_page(status="retired"))),
            ["retired-without-date", "retired-without-reason"],
        )

    def test_retired_reason_must_be_non_empty_string(self):
        self.assertEqual(
            _codes(_page(status="retired", retired_reason="  ", retired_at="2026-11-02")),
            ["retired-without-reason"],
        )
        self.assertEqual(
            _codes(_page(status="retired", retired_reason=["x"], retired_at="2026-11-02")),
            ["retired-without-reason"],
        )

    def test_retired_at_must_be_iso_date(self):
        self.assertEqual(
            _codes(_page(status="retired", retired_reason="Replaced by habit.", retired_at="11/02/2026")),
            ["invalid-retired-at"],
        )
        self.assertEqual(
            _codes(_page(status="retired", retired_reason="Replaced by habit.", retired_at=date(2026, 11, 2))),
            [],
        )

    def test_retired_page_is_valid_without_superseded_by(self):
        page = _page(status="retired", retired_reason="No longer true.", retired_at="2026-11-02")
        self.assertEqual(_codes(page), [])
        self.assertEqual(validate_schema(page), [])

    # Rule 5
    def test_retired_fields_only_on_retired_pages(self):
        self.assertEqual(_codes(_page(retired_reason="Why.")), ["retired-field-without-retired-status"])
        self.assertEqual(_codes(_page(retired_at="2026-11-02")), ["retired-field-without-retired-status"])
        self.assertEqual(
            _codes(_page(status="deprecated", retired_reason="Why.", retired_at="2026-11-02")),
            ["retired-field-without-retired-status", "retired-field-without-retired-status"],
        )

    def test_issues_are_errors_with_page_path(self):
        issues = validate_memory_fields(_page(pin=True))
        self.assertEqual([(issue.severity, issue.path) for issue in issues], [("error", "wiki/concepts/memory.md")])


def _fixture_vault(root: Path) -> None:
    shutil.copytree(REPO / "wiki", root / "wiki")
    shutil.copytree(REPO / "raw", root / "raw")
    for folder in ("drafts", "reports", "review"):
        (root / "scratch" / folder).mkdir(parents=True, exist_ok=True)


def _add_indexed_page(root: Path, slug: str, frontmatter: dict, body: str) -> None:
    (root / "wiki" / "concepts" / f"{slug}.md").write_text(render_page(frontmatter, body))
    index = root / "wiki" / "index.md"
    index.write_text(index.read_text().rstrip() + f"\n- [[concepts/{slug}]]\n")


MEMORY_CODES = {
    "memory-field-without-schema",
    "invalid-memory-schema",
    "invalid-pin",
    "invalid-memory-kind",
    "retired-without-reason",
    "retired-without-date",
    "invalid-retired-at",
    "retired-field-without-retired-status",
}


class LintRepoMemoryFieldsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        _fixture_vault(self.root)

    def _error_codes(self) -> list[str]:
        return [issue.code for issue in lint_repo(self.root).issues if issue.severity == "error"]

    def test_vault_without_new_fields_still_passes_lint(self):
        self.assertEqual(lint_repo(self.root).issues, [])

    def test_lint_reports_each_contract_case(self):
        cases = {
            "pin-yes": (_frontmatter("Pin Yes", memory_schema=MEMORY_SCHEMA, pin="yes"), "invalid-pin"),
            "bad-kind": (
                _frontmatter("Bad Kind", memory_schema=MEMORY_SCHEMA, memory_kind="fact"),
                "invalid-memory-kind",
            ),
            "pin-no-schema": (_frontmatter("Pin No Schema", pin=True), "memory-field-without-schema"),
            "retired-no-reason": (
                _frontmatter("Retired No Reason", status="retired", retired_at="2026-11-02"),
                "retired-without-reason",
            ),
        }
        for slug, (frontmatter, code) in cases.items():
            _add_indexed_page(self.root, slug, frontmatter, f"# {frontmatter['title']}\n\nMemory text.\n")
        codes = self._error_codes()
        for slug, (_, code) in cases.items():
            with self.subTest(slug=slug):
                self.assertIn(code, codes)
        self.assertEqual(sorted(c for c in codes if c in MEMORY_CODES), sorted(code for _, code in cases.values()))

    def test_retired_page_without_superseded_by_passes_lint(self):
        _add_indexed_page(
            self.root,
            "retired-memory",
            _frontmatter(
                "Retired Memory",
                status="retired",
                memory_schema=MEMORY_SCHEMA,
                memory_kind="preference",
                retired_reason="The user changed this preference.",
                retired_at="2026-11-02",
            ),
            "# Retired Memory\n\nOld preference.\n",
        )
        self.assertEqual(self._error_codes(), [])

    def test_deprecated_still_needs_superseded_by(self):
        _add_indexed_page(
            self.root,
            "deprecated-memory",
            _frontmatter("Deprecated Memory", status="deprecated"),
            "# Deprecated Memory\n\nOld.\n",
        )
        self.assertIn("deprecated-page-without-superseded-by", self._error_codes())


def _retired(title: str, page_type: str = "concept", **extra: object) -> dict:
    return _frontmatter(
        title,
        page_type,
        status="retired",
        retired_reason="No longer current.",
        retired_at="2026-11-02",
        **extra,
    )


class RetiredIsNotCurrentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        _fixture_vault(self.root)

    def test_search_skips_retired_unless_history_is_asked(self):
        from tools.wiki.lib.search import search_wiki

        _add_indexed_page(self.root, "zebra-retired", _retired("Zebrafish Retired"), "# Zebrafish\n\nZebrafish note.\n")
        current = search_wiki(self.root, "Zebrafish")
        history = search_wiki(self.root, "Zebrafish", include_deprecated=True)
        self.assertEqual([hit.path for hit in current], [])
        self.assertEqual([hit.path for hit in history], ["wiki/concepts/zebra-retired.md"])

    def test_lifecycle_map_lists_retired_pages(self):
        from tools.wiki.lib.maps import _render_lifecycle_map

        _add_indexed_page(self.root, "old-habit", _retired("Old Habit"), "# Old Habit\n\nOld.\n")
        page = parse_page(self.root / "wiki" / "concepts" / "old-habit.md")
        text = _render_lifecycle_map(self.root, [page], [])
        not_current = text.split("## Deprecated Pages", 1)[1].split("##", 1)[0]
        self.assertIn("concepts/old-habit", not_current)

    def test_project_summary_skips_retired_decisions(self):
        from tools.wiki.lib.maps import _project_summary_pages

        (self.root / "wiki" / "decisions").mkdir(exist_ok=True)
        current = self.root / "wiki" / "decisions" / "keep.md"
        current.write_text(render_page(_frontmatter("Keep", "decision", project="demo"), "# Keep\n"))
        retired = self.root / "wiki" / "decisions" / "drop.md"
        retired.write_text(render_page(_retired("Drop", "decision", project="demo"), "# Drop\n"))
        summaries = _project_summary_pages(self.root, [parse_page(current), parse_page(retired)], [], date(2026, 11, 2))
        body = "\n".join(page.body for page in summaries)
        self.assertIn("decisions/keep", body)
        self.assertNotIn("decisions/drop", body)

    def test_metrics_count_links_to_retired_pages(self):
        from tools.wiki.lib.metrics import _deprecated_linked_targets

        target = self.root / "wiki" / "concepts" / "gone.md"
        target.write_text(render_page(_retired("Gone"), "# Gone\n"))
        linker = self.root / "wiki" / "concepts" / "linker.md"
        linker.write_text(render_page(_frontmatter("Linker"), "# Linker\n\nSee [[concepts/gone]].\n"))
        pages = [parse_page(target), parse_page(linker)]
        self.assertEqual(_deprecated_linked_targets(self.root, pages), ["concepts/gone"])

    def test_merge_scan_skips_retired_pages(self):
        from tools.wiki.lib.merge import _find_merge_candidates

        concepts = self.root / "wiki" / "concepts"
        (concepts / "twin-a.md").write_text(render_page(_frontmatter("Twin Unique Title"), "# A\n"))
        (concepts / "twin-b.md").write_text(render_page(_retired("Twin Unique Title"), "# B\n"))
        keys = [candidate.key for candidate in _find_merge_candidates(self.root)]
        self.assertNotIn("title:twin-unique-title", keys)


if __name__ == "__main__":
    unittest.main()
