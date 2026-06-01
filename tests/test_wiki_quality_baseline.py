import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WIKI = ROOT / "wiki"


REQUIRED_FRONTMATTER_FIELDS = {
    "title",
    "type",
    "status",
    "created",
    "updated",
    "owner",
    "summary",
    "source_count",
    "tags",
    "related",
    "confidence",
    "quality",
}

REQUIRED_QUALITY_FIELDS = {
    "provenance",
    "links",
    "contradictions",
    "review_required",
}

EXPECTED_INITIAL_PAGES = [
    "wiki/overview.md",
    "wiki/index.md",
    "wiki/log.md",
    "wiki/inbox.md",
    "wiki/concepts/compiled-knowledge.md",
    "wiki/concepts/llm-maintained-wiki.md",
    "wiki/concepts/source-of-truth.md",
    "wiki/workflows/ingest-new-source.md",
    "wiki/workflows/wiki-linting.md",
    "wiki/decisions/use-markdown-for-compiled-knowledge.md",
    "wiki/maps/wiki-operating-map.md",
    "wiki/maps/topic-map.md",
    "wiki/maps/source-map.md",
    "wiki/maps/decision-map.md",
    "wiki/maps/review-map.md",
    "wiki/maps/lifecycle-map.md",
]


def split_frontmatter(text):
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return None
    return match.group(1)


def top_level_keys(frontmatter):
    keys = set()
    for line in frontmatter.splitlines():
        if not line.strip() or line.startswith(" ") or line.startswith("-"):
            continue
        key = line.split(":", 1)[0].strip()
        keys.add(key)
    return keys


def nested_quality_keys(frontmatter):
    keys = set()
    in_quality = False
    for line in frontmatter.splitlines():
        if line.startswith("quality:"):
            in_quality = True
            continue
        if in_quality and line and not line.startswith(" "):
            break
        if in_quality and line.startswith("  ") and ":" in line:
            keys.add(line.split(":", 1)[0].strip())
    return keys


class WikiQualityBaselineTest(unittest.TestCase):
    def test_expected_initial_pages_exist(self):
        missing = [path for path in EXPECTED_INITIAL_PAGES if not (ROOT / path).exists()]
        self.assertEqual([], missing)

    def test_wiki_markdown_pages_have_required_frontmatter(self):
        pages = sorted(WIKI.rglob("*.md"))
        self.assertGreater(len(pages), 0)

        failures = []
        for page in pages:
            frontmatter = split_frontmatter(page.read_text())
            if frontmatter is None:
                failures.append(f"{page.relative_to(ROOT)}: missing frontmatter")
                continue

            missing_fields = REQUIRED_FRONTMATTER_FIELDS - top_level_keys(frontmatter)
            missing_quality = REQUIRED_QUALITY_FIELDS - nested_quality_keys(frontmatter)
            if missing_fields:
                failures.append(
                    f"{page.relative_to(ROOT)}: missing fields {sorted(missing_fields)}"
                )
            if missing_quality:
                failures.append(
                    f"{page.relative_to(ROOT)}: missing quality fields {sorted(missing_quality)}"
                )

        self.assertEqual([], failures)

    def test_index_lists_initial_pages(self):
        index = (WIKI / "index.md").read_text()
        for section in [
            "## Overview",
            "## Sources",
            "## Concepts",
            "## Entities",
            "## Systems",
            "## Workflows",
            "## Decisions",
            "## Maps",
        ]:
            self.assertIn(section, index)

        for link in [
            "[[overview]]",
            "[[concepts/compiled-knowledge]]",
            "[[concepts/llm-maintained-wiki]]",
            "[[concepts/source-of-truth]]",
            "[[workflows/ingest-new-source]]",
            "[[workflows/wiki-linting]]",
            "[[decisions/use-markdown-for-compiled-knowledge]]",
            "[[maps/wiki-operating-map]]",
            "[[maps/topic-map]]",
            "[[maps/source-map]]",
            "[[maps/decision-map]]",
            "[[maps/review-map]]",
            "[[maps/lifecycle-map]]",
        ]:
            self.assertIn(link, index)

    def test_log_has_parseable_initial_entry(self):
        log = (WIKI / "log.md").read_text()
        self.assertRegex(log, r"## \[2026-05-26\] (repair|decision|schema-change) \| .+")

    def test_agents_entrypoint_is_concise_and_links_full_schema(self):
        agents = (ROOT / "AGENTS.md").read_text()
        self.assertLessEqual(len(agents.splitlines()), 180)
        self.assertIn("docs/agent/OPERATING-SCHEMA.md", agents)
        self.assertIn("agents/skills/", agents)
        self.assertIn("wiki-ingest", agents)
        self.assertIn("wiki-update", agents)
        self.assertIn("wiki-query", agents)
        self.assertIn("publish-draft", agents)
        self.assertIn("health", agents)
        self.assertTrue((ROOT / "docs" / "agent" / "OPERATING-SCHEMA.md").exists())

    def test_release_gate_script_declares_full_validation_contract(self):
        gate = ROOT / "scripts" / "release_gate.sh"

        self.assertTrue(gate.exists(), "scripts/release_gate.sh must exist")
        self.assertTrue(gate.stat().st_mode & 0o111, "release gate must be executable")

        text = gate.read_text()
        for token in [
            "set -euo pipefail",
            "PYTHON_BIN=\"$(choose_python)\"",
            "$PYTHON_BIN -m unittest tests/test_wiki_quality_baseline.py tests/test_wiki_tools.py",
            "$PYTHON_BIN tools/wiki/cli.py lint --report",
            "$PYTHON_BIN tools/wiki/cli.py health",
            "$PYTHON_BIN tools/wiki/cli.py maps build --check --report",
            "$PYTHON_BIN tools/wiki/cli.py metrics --check --report",
            "validate-stenc-doc.js",
            "setup-project.js",
            "check-rendered-pages.js",
            "tools/stenc/validate-stenc-doc.js",
            "tools/stenc/setup-project.js",
            "tools/stenc/check-rendered-pages.js",
            "mktemp -d",
            "rsync -a",
            'git -C "$ROOT" rev-parse --show-toplevel',
            "No Git worktree detected",
            "check_workspace_whitespace",
            'git -C "$ROOT" diff --no-ext-diff --binary -- .',
            "Release gate changed tracked output",
            'git -C "$ROOT" diff --check -- .',
        ]:
            self.assertIn(token, text)

        self.assertNotIn("$HOME/.codex/skills/stenc", text)
        for script in [
            ROOT / "tools" / "stenc" / "validate-stenc-doc.js",
            ROOT / "tools" / "stenc" / "setup-project.js",
            ROOT / "tools" / "stenc" / "check-rendered-pages.js",
        ]:
            self.assertTrue(script.exists(), f"{script.relative_to(ROOT)} must exist")
        self.assertIn("scripts/release_gate.sh", (ROOT / ".github" / "workflows" / "release-gate.yml").read_text())

        for doc in [
            "README.md",
            "AGENTS.md",
            "docs/LLM-WIKI.md",
            "docs/usage.md",
            "tools/wiki/README.md",
        ]:
            self.assertIn("scripts/release_gate.sh", (ROOT / doc).read_text())

    def test_repo_local_wiki_skill_docs_exist_and_preserve_boundary(self):
        skill_paths = [
            ROOT / "agents" / "skills" / "wiki-ingest" / "SKILL.md",
            ROOT / "agents" / "skills" / "wiki-update" / "SKILL.md",
            ROOT / "agents" / "skills" / "wiki-query" / "SKILL.md",
        ]

        missing = [str(path.relative_to(ROOT)) for path in skill_paths if not path.exists()]
        self.assertEqual([], missing)

        for path in skill_paths:
            text = path.read_text()
            self.assertIn("Skill = cognition", text)
            self.assertIn("CLI = mutation and validation", text)
            self.assertIn("publish-draft", text)
            self.assertNotIn("when available", text)
            self.assertNotIn("Until `publish-draft` is available", text)
            self.assertNotIn("wiki-capture", text)

    def test_draft_guide_uses_publish_draft_as_durable_gate(self):
        text = (ROOT / "docs" / "agent" / "DRAFTS.md").read_text()

        self.assertIn("publish-draft", text)
        self.assertIn("durable publication", text)
        self.assertNotIn("apply-draft scratch/drafts/example.json --report", text)

    def test_usage_guide_does_not_reintroduce_apply_draft_as_publish_path(self):
        text = (ROOT / "docs" / "usage.md").read_text()

        self.assertNotIn("apply-draft scratch/drafts/example.json --report", text)
        self.assertNotIn("| draft 적용 | `python3 tools/wiki/cli.py apply-draft", text)

    def test_wiki_skill_docs_are_hardened_for_agent_execution(self):
        ingest = (ROOT / "agents" / "skills" / "wiki-ingest" / "SKILL.md").read_text()
        update = (ROOT / "agents" / "skills" / "wiki-update" / "SKILL.md").read_text()
        query = (ROOT / "agents" / "skills" / "wiki-query" / "SKILL.md").read_text()

        for token in [
            "same canonical_source and same raw_sha256",
            "source-drift",
            "slug-collision",
            "source_count",
            "quality.provenance",
        ]:
            self.assertIn(token, ingest)

        for token in [
            "exact content preservation",
            "scratch/reports/",
            "scratch/review/",
            "tools/wiki/templates/draft-upsert-page.json",
            "index and log",
        ]:
            self.assertIn(token, update)

        for token in [
            "scratch/reports/YYYY-MM-DD-query-<slug>.md",
            "Pages Consulted",
            "Sources Consulted",
            "Reusable Capture Recommendation",
            "Substantive synthesized answers require citations",
        ]:
            self.assertIn(token, query)


if __name__ == "__main__":
    unittest.main()
