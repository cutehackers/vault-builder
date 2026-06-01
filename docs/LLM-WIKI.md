# LLM Wiki Final Architecture

## Purpose

This document defines the final improved shape of this vault as a quality-gated LLM Wiki.

The goal is not to build a passive RAG folder. The goal is to maintain a durable, compounding, human-browsable markdown wiki where raw sources stay immutable, LLM agents propose semantic knowledge, deterministic tooling enforces structural quality, and humans resolve judgment-heavy questions.

## Root Cause

The core quality risk is not lack of folders, prompts, or agent access.

The root cause is this:

```text
Without an enforced quality boundary, LLM-generated text can look like durable knowledge before it has provenance, links, contradiction handling, index coverage, or human-review state.
```

Therefore the architecture must not rely on agent discipline alone. It must make invalid wiki states visible and block completion claims until validation passes.

## Final Model

This vault uses a Quality-Gated Compiled Wiki model.

```text
Raw sources are immutable.
LLM drafts are semantic proposals.
Wiki pages are compiled knowledge.
Quality gates are mandatory.
Human review resolves judgment.
Git preserves durable state.
```

The durable boundary is:

```text
raw source
  -> LLM semantic draft
  -> deterministic validation
  -> compiled wiki page
  -> index, links, log, and reports
```

## Topology

```text
.
├── AGENTS.md
├── docs/
│   ├── LLM-WIKI.md
│   └── stenc/
├── raw/
│   ├── sources/
│   ├── assets/
│   └── imports/
├── wiki/
│   ├── index.md
│   ├── log.md
│   ├── inbox.md
│   ├── overview.md
│   ├── sources/
│   ├── concepts/
│   ├── entities/
│   ├── systems/
│   ├── workflows/
│   ├── decisions/
│   └── maps/
├── scratch/
│   ├── drafts/
│   ├── reports/
│   └── review/
└── tools/
    └── wiki/
```

## Layer Responsibilities

| Layer | Responsibility | Durable? |
|---|---|---:|
| `raw/` | Immutable source material | Yes |
| `scratch/drafts/` | LLM semantic proposals before validation | No |
| `scratch/reports/` | Operational audit artifacts for ingest, query, lint, audit, and repair work | No |
| `scratch/review/` | Pending human-judgment items | No |
| `wiki/` | Compiled, linked, provenance-backed knowledge | Yes |
| `tools/wiki/` | Deterministic validation, mutation gates, workflow checkpoints, MCP wrappers, maps, and metrics tooling | Yes |
| `docs/stenc/` | Fixed-format specs and plans for implementation work | Yes |

## Non-Negotiable Boundaries

### Raw Sources

Files under `raw/` are source truth. Agents may read them but must not edit, rename, rewrite, or delete them unless explicitly instructed.

If a raw source contains an error, the correction belongs in `wiki/`, not in the raw source.

### Scratch

Files under `scratch/drafts/` and `scratch/review/` are not durable knowledge.

Wiki pages must not cite scratch drafts as source truth. A draft becomes durable only after it is validated and promoted into `wiki/`.

### Wiki

Files under `wiki/` are compiled knowledge artifacts.

Every substantive claim should be traceable to raw sources, source pages, prior wiki pages, or explicit human instruction.

### Tools

`tools/wiki/` owns deterministic quality checks.

The validator does not decide whether a claim is true. It checks whether the page has the required structure, links, provenance signals, log format, and review state.

## Frontmatter Contract

Every durable wiki page must start with this common frontmatter shape:

```yaml
---
title: "Human-readable title"
type: source | concept | entity | system | workflow | decision | map | claim | comparison | timeline | question | glossary | index | log | inbox | overview
status: seed | active | stable | contested | deprecated
created: YYYY-MM-DD
updated: YYYY-MM-DD
owner: agent | human
summary: "One-sentence indexable summary."
source_count: 0
tags: []
related: []
confidence: low | medium | high
quality:
  provenance: none | partial | section | claim
  links: unchecked | valid | broken
  contradictions: unchecked | none | present | contested
  review_required: false
---
```

Optional common fields:

```yaml
aliases: []
primary_sources: []
derived_from: []
review_after: YYYY-MM-DD
supersedes: []
superseded_by: []
```

Source pages may add:

```yaml
canonical_source: "raw/sources/..."
source_kind: markdown | pdf | web | transcript | image | code | other
source_date: YYYY-MM-DD
authors: []
url: ""
raw_sha256: ""
```

Decision pages may add:

```yaml
decision_status: proposed | accepted | superseded | rejected
decided_at: YYYY-MM-DD
```

Claim pages may add:

```yaml
claim_status: stated | inferred | contested | deprecated | speculative
evidence_level: weak | medium | strong
```

## Frontmatter Boundary

Frontmatter is for routing, linting, indexing, and quality triage.

It must not become a hidden knowledge database.

Keep these in the markdown body:

- claim-by-claim evidence
- contradiction explanations
- rationale for confidence
- decision options and tradeoffs
- source takeaways
- follow-up questions
- detailed lint results

`source_count` is metadata, not evidence. Claim evidence belongs near the claim.

When `quality.provenance` is `claim`, the body must include a claim evidence table:

```markdown
| Claim | Status | Evidence |
|---|---|---|
| Durable claim text. | stated | `raw/sources/example.md` |
```

Allowed claim row statuses are `stated`, `inferred`, `contested`, and `deprecated`. Evidence should point to raw paths, source pages, wiki pages, or explicit human instruction. Raw evidence must either have a registered source page with a valid `raw_sha256` or include an inline `#sha256=...` suffix.

## Quality Gates

A wiki operation is not complete until these conditions are satisfied:

- Raw sources were not modified.
- Relevant existing pages were checked before creating new pages.
- Durable pages have required frontmatter.
- Important claims have provenance signals.
- Broken wiki links are reported or fixed.
- Contradictions are recorded, not silently resolved.
- Human-review items are moved to `scratch/review/` when judgment is required.
- `wiki/index.md` is updated when navigation changes.
- `wiki/log.md` is appended for meaningful operations.
- A report is written under `scratch/reports/` for non-trivial ingest, lint, audit, or repair work.
- `tools/wiki` validation passes before claiming completion.

## Recommended Skill Workflows

The final human-facing workflow is intentionally simpler than the internal quality pipeline.

Humans should only need to remember three workflow names:

| Workflow | Purpose | Current State |
|---|---|---|
| `wiki-ingest` | Reflect a new raw source into the wiki. | Implemented as repo-local skill documentation under `agents/skills/wiki-ingest/`. |
| `wiki-update` | Update an existing durable wiki page from sources or human instruction. | Implemented as repo-local skill documentation under `agents/skills/wiki-update/`. |
| `wiki-query` | Answer questions from the wiki and optionally file reusable answers back. | Implemented as repo-local skill documentation under `agents/skills/wiki-query/`. |

Query modes:

| Mode | Result |
|---|---|
| `answer-only` | Conversation answer only. |
| `answer-with-report` | Conversation answer plus `scratch/reports/YYYY-MM-DD-query-<slug>.md` query report. |
| `answer-and-capture` | Reusable answer captured as a draft and published through validation when appropriate. |

The responsibility split is mandatory:

```text
Skill = cognition
CLI = mutation and validation
MCP = CLI semantics wrapper
Wiki/Git = durable state
```

Skills inspect sources, synthesize knowledge, detect duplicates and contradictions, and write drafts or reports. Durable wiki mutation must go through `tools/wiki` validation.

## Implemented Validator

Current commands:

```bash
python3 tools/wiki/cli.py validate-page wiki/overview.md
python3 tools/wiki/cli.py hash-source raw/sources/example.md
python3 tools/wiki/cli.py ingest-source raw/sources/example.md --report
python3 tools/wiki/cli.py source-registry --report
python3 tools/wiki/cli.py bulk-ingest raw/sources/a.md raw/sources/b.md --report
python3 tools/wiki/cli.py publish-draft scratch/drafts/example.json --report
python3 tools/wiki/cli.py publish-batch scratch/drafts/example-batch.json --report
python3 tools/wiki/cli.py review create --type contradiction --summary "Example Review" --related wiki/overview.md --context "Why human judgment is needed."
python3 tools/wiki/cli.py review list
python3 tools/wiki/cli.py review resolve scratch/review/example-review.md --status accepted --resolution "Accepted with rationale."
python3 tools/wiki/cli.py merge scan --report --create-review
python3 tools/wiki/cli.py maps build --report
python3 tools/wiki/cli.py maps build --check --report
python3 tools/wiki/cli.py metrics --check --report
python3 tools/wiki/cli.py health
python3 tools/wiki/cli.py apply-draft scratch/drafts/example.json --dry-run
python3 tools/wiki/cli.py workflow ingest --source raw/sources/example.md --report
python3 tools/wiki/cli.py workflow update --target wiki/concepts/example.md --preflight --report
python3 tools/wiki/cli.py workflow query --question "..." --prepare-report
python3 tools/wiki/cli.py workflow query --question "..." --mode answer-with-report --report
python3 tools/wiki/cli.py workflow query --question "..." --mode answer-and-capture --draft scratch/drafts/query-answer.json --report
python3 tools/wiki/mcp_server.py
python3 tools/wiki/cli.py lint
python3 tools/wiki/cli.py lint --report scratch/reports/YYYY-MM-DD-lint.md
```

Current checks:

- required frontmatter fields
- required `quality` fields
- required field types for `source_count`, `tags`, `related`, and `quality.review_required`
- enum values for page metadata
- Obsidian-style wiki link resolution with `wiki/` root boundary checks
- required core index links
- orphan durable pages not linked from index or maps
- parseable log event prefixes
- raw source drift when `canonical_source` and `raw_sha256` are present
- basic provenance signals
- claim-level provenance table shape, status values, evidence refs, `source_count`, and raw/source hash connection
- unmatched human-lock markers
- source registry status for registered, unregistered, drift, and missing raw sources
- bulk source registration outcome reports
- source registration no-clobber for unchanged, drift, collision, and locked source pages
- section-level human-lock exact content preservation during draft application
- review queue item schema and lifecycle status validation
- lifecycle metadata checks for aliases, redirects, supersedes, superseded_by, and deprecated pages
- duplicate candidate scan with merge proposal reports and optional review item creation
- query report mode and reusable answer capture draft scaffolding
- generated navigation maps for topics, sources, decisions, pending reviews, and lifecycle signals
- stale generated map detection through `maps build --check`
- maintenance metrics dashboard for pending reviews, contested claims, stale sources, orphan pages, provenance coverage, deprecated links, and last health report
- configurable maintenance metric thresholds through `tools/wiki/metrics-policy.json` or `metrics --policy`
- report and generated query-capture draft path confinement under `scratch/reports/` and `scratch/drafts/`
- release gate wrapper for tests, lint, health, map freshness, metrics thresholds, Stenc validation/render checks, and `git diff --check` in a temporary repo copy

Intentional limits:

- It does not arbitrate truth.
- It does not merge pages.
- It does not auto-fix content.
- It does not mutate `raw/`.
- `ingest-source` registers a source page; semantic extraction remains agent-authored draft work.
- `source-registry` reports raw/source-page inventory; it does not write semantic wiki content.
- `bulk-ingest` registers multiple raw sources; semantic extraction remains agent-authored draft work.
- workflow commands are deterministic checkpoints; they do not perform semantic extraction or answer synthesis.
- `publish-draft` requires a clean wiki before applying, runs dry-run validation, applies the draft, runs lint/report, and rolls back page/index/log changes if post-apply lint fails.
- `publish-batch` applies the same gate to multi-page batch drafts and snapshots all target pages plus index/log before mutation.
- Batch page entries use the page draft shape without page-level `log`; the top-level batch `log` records the operation once.
- Claim-level provenance validation checks structure and evidence linkage; it does not decide whether a claim is true.
- `review` commands create, list, and resolve queue items; they record human decisions but do not make the decision automatically.
- `merge scan` proposes duplicate candidates and can open a merge review item; it never merges, redirects, or deprecates pages automatically.
- `maps build` deterministically rebuilds generated navigation maps and can fail in `--check` mode when generated map pages are stale.
- `metrics` writes an operational dashboard; it reports maintenance pressure but does not decide or repair content.
- `workflow query --mode` persists supplied answer summaries, consulted context, confidence, contradictions, and capture drafts; it does not synthesize answers automatically.
- `health` runs lint with the default dated report path and returns the same success/failure status as lint.
- `mcp_server.py` exposes stable commands and facade commands as stdio MCP tools.

## Workflows

### Ingest

```text
identify source
  -> create source metadata
  -> extract durable knowledge
  -> compare with existing wiki
  -> prepare ingest report
  -> apply validated updates
  -> update index and log
```

Ingest must be hybrid:

- LLM extracts semantics.
- CLI validates and writes.
- Human resolves important ambiguity.

### Query

```text
read index
  -> read relevant pages
  -> inspect source pages or raw sources if needed
  -> answer with synthesis
  -> optionally file durable answer back into wiki
```

A useful query result should become wiki content when it creates reusable comparison, explanation, workflow, decision, or map value.

### Lint

```text
discover wiki pages
  -> validate frontmatter
  -> validate links
  -> validate index coverage
  -> validate log format
  -> validate provenance signals
  -> report issues
```

Lint is structural. It detects unsupported states; it does not make human judgments.

### Review

Human review is required when:

- sources conflict on an important claim
- a page merge would change meaning
- a decision would be superseded
- a claim is high-impact and contested
- a human-locked section needs modification

## Architecture Sequence

The correct build order is:

1. Normalize baseline wiki pages. Done.
2. Implement deterministic validation. Done.
3. Define semantic draft schema. Done for JSON `upsert-page` drafts.
4. Implement source hashing. Done.
5. Implement `apply-draft`. Done for validated page upserts.
6. Implement deterministic source registration. Done as `ingest-source`.
7. Add semantic extraction templates for source-to-draft work. Done as `docs/agent/DRAFTS.md` and `tools/wiki/templates/draft-upsert-page.json`.
8. Wrap stable CLI behavior with MCP. Done as `tools/wiki/mcp_server.py`.
9. Define simplified skill workflows. Done as Stenc spec `2026-05-29-wiki-skill-workflows`.
10. Implement `publish-draft` and `health` CLI facade commands. Done.
11. Add `wiki-ingest`, `wiki-update`, and `wiki-query` repo-local skill docs. Done.
12. Expose `wiki_publish_draft` and `wiki_health` via MCP. Done.
13. Harden workflow execution with no-clobber ingest, section-lock preservation, query report scaffolds, and workflow CLI/MCP checkpoint tools. Done.
14. Add source registry and bulk source registration. Done.
15. Add transactional multi-page publish with batch draft template and MCP facade. Done.
16. Add deterministic claim-level provenance validation for claim tables, evidence refs, source counts, and source hash linkage. Current deterministic baseline implemented.
17. Add deterministic human-review queue create/list/resolve commands with resolution log entries and MCP facade. Current deterministic baseline implemented.
18. Add deterministic merge/supersession lifecycle validation and duplicate scan reports with optional merge review item creation. Current deterministic baseline implemented.
19. Add deterministic query-to-knowledge report and reusable answer capture draft scaffolding. Current deterministic baseline implemented.
20. Add generated navigation maps for topics, sources, decisions, unresolved reviews, and lifecycle signals. Current deterministic baseline implemented.
21. Add sustainable maintenance metrics dashboard and MCP facade. Current deterministic baseline implemented.

MCP is intentionally thin. It exposes the stable local contract; it does not define the contract.

## Source Of Truth Hierarchy

This vault has two priority hierarchies.

For operating behavior, use this hierarchy:

1. Explicit human instruction in the current task.
2. `AGENTS.md` operating contract.
3. `docs/LLM-WIKI.md` architecture definition.
4. Stenc specs and plans under `docs/stenc/content/`.
5. Durable wiki pages with provenance.

For factual evidence, use this hierarchy:

1. Raw files under `raw/`.
2. Source pages under `wiki/sources/`.
3. Durable synthesis pages under `wiki/`.
4. `scratch/reports/` as operational audit history, not durable claim evidence.
5. `scratch/drafts/` and `scratch/review/` as temporary staging only.

Contradictions must be recorded before replacing an older view.

## Current Canonical Documents

- `AGENTS.md` defines the agent operating contract.
- `docs/LLM-WIKI.md` defines the final architecture.
- `docs/stenc/content/specs/2026-05-26-wiki-quality-validator.spec.json` defines validator behavior.
- `docs/stenc/content/plans/2026-05-26-wiki-quality-validator-implementation.plan.json` defines validator implementation order.
- `wiki/overview.md` is the human-readable wiki briefing.
- `wiki/index.md` is the page catalog.
- `wiki/log.md` is the chronological operation log.

## Definition Of Done

For architecture or wiki-maintenance work, done means:

```bash
scripts/release_gate.sh
```

The release gate runs unit tests, lint, health, map freshness, metrics, and Stenc validation/render checks in a temporary repo copy, then checks the working tree diff for whitespace issues:

```bash
python3 -m unittest tests/test_wiki_quality_baseline.py tests/test_wiki_tools.py
python3 tools/wiki/cli.py lint --report
python3 tools/wiki/cli.py health
python3 tools/wiki/cli.py maps build --check --report
python3 tools/wiki/cli.py metrics --check --report
node tools/stenc/validate-stenc-doc.js <doc.json>
node tools/stenc/setup-project.js --project-root "$(pwd)" --docs-dir docs/stenc
node tools/stenc/check-rendered-pages.js docs/stenc
git diff --check
```

The same gate is wired into `.github/workflows/release-gate.yml`.

No completion claim should be made without fresh verification output.
