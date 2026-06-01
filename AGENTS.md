# AGENTS.md - LLM Wiki Agent Contract

This vault is a quality-gated compiled markdown wiki.

Read this file first. Read the longer reference only when the task needs detail:

- Full operating schema: `docs/agent/OPERATING-SCHEMA.md`
- Architecture: `docs/LLM-WIKI.md`
- Skill workflows: `agents/skills/`
- Draft guide: `docs/agent/DRAFTS.md`
- Human guide: `README.md`

## Mission

Do not treat this as a passive RAG folder.

Maintain durable, linked, provenance-backed wiki pages:

```text
raw source
-> LLM semantic draft
-> deterministic validation
-> durable wiki page
-> index / links / log / reports
-> Git history
```

## Non-Negotiable Rules

1. Never edit, rename, rewrite, or delete files under `raw/` unless the human explicitly asks.
2. Never present LLM draft text as durable knowledge before validation.
3. Every substantive wiki claim needs provenance from a raw source, source page, prior wiki page, or explicit human instruction.
4. Do not silently resolve contradictions. Record the tension and request human judgment when it affects an important decision.
5. Preserve `<!-- human-locked -->` pages and `<!-- human-locked:start -->` sections.
6. Update `wiki/index.md` and `wiki/log.md` for meaningful durable changes.
7. Run deterministic validation before claiming wiki work is complete.

## Directory Map

| Path | Meaning |
|---|---|
| `raw/` | Immutable source material. |
| `wiki/` | Durable compiled knowledge. |
| `scratch/drafts/` | LLM proposals before validation. |
| `scratch/reports/` | Lint, ingest, audit, query, and repair reports. |
| `scratch/review/` | Human-judgment queue. |
| `tools/wiki/` | Deterministic quality tooling. |
| `agents/skills/` | Agent workflow guides for `wiki-ingest`, `wiki-update`, and `wiki-query`. |
| `docs/agent/` | Detailed agent reference. |

## Required Commands

Run the full release gate:

```bash
scripts/release_gate.sh
```

Validate one page:

```bash
python3 tools/wiki/cli.py validate-page wiki/overview.md
```

Source commands:

```bash
python3 tools/wiki/cli.py hash-source raw/sources/example.md
python3 tools/wiki/cli.py ingest-source raw/sources/example.md --report
python3 tools/wiki/cli.py source-registry --report
python3 tools/wiki/cli.py bulk-ingest raw/sources/a.md raw/sources/b.md --report
```

Run workflow checkpoints:

```bash
python3 tools/wiki/cli.py workflow ingest --source raw/sources/example.md --report
python3 tools/wiki/cli.py workflow update --target wiki/overview.md --preflight --report
python3 tools/wiki/cli.py workflow query --question "..." --mode answer-with-report --report
```

Apply a validated semantic draft:

```bash
python3 tools/wiki/cli.py publish-draft scratch/drafts/example.json --report
python3 tools/wiki/cli.py publish-batch scratch/drafts/example-batch.json --report
python3 tools/wiki/cli.py merge scan --report --create-review
python3 tools/wiki/cli.py maps build --check --report
python3 tools/wiki/cli.py metrics --check --report
python3 tools/wiki/cli.py review list
```

Run a whole-vault health check:

```bash
python3 tools/wiki/cli.py health
```

Run the MCP wrapper:

```bash
python3 tools/wiki/mcp_server.py
```

## Minimal Frontmatter

Every durable page under `wiki/` starts with frontmatter containing:

```yaml
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
```

Source pages should also use:

```yaml
canonical_source: "raw/sources/..."
raw_sha256: "..."
```

## Task Routing

When asked to ingest:

1. Follow `agents/skills/wiki-ingest/SKILL.md` when workflow detail is needed.
2. Read `wiki/index.md`.
3. Hash or register the raw source with `ingest-source` or `workflow ingest`.
4. Create a semantic draft under `scratch/drafts/` using `tools/wiki/templates/draft-upsert-page.json`.
5. Publish it with `tools/wiki/cli.py publish-draft`.
6. Run `health` or lint and write a report.

When asked to update:

1. Follow `agents/skills/wiki-update/SKILL.md`.
2. Read the target page, related pages, and supporting sources.
3. Preserve human locks and record contradictions. Use `workflow update --preflight` when a deterministic checkpoint helps.
4. File safe changes through a draft and `publish-draft`.

When asked to query:

1. Follow `agents/skills/wiki-query/SKILL.md` when reusable capture or report output is requested.
2. Read `wiki/index.md`.
3. Read the relevant wiki pages.
4. Inspect source pages or raw sources only when needed.
5. Answer with synthesis and cite durable pages/sources.
6. File reusable results back through a draft and `publish-draft` if they should become durable. Use `workflow query --prepare-report` for reusable query reports.

When asked to lint or repair:

1. Run `python3 tools/wiki/cli.py lint --report`.
2. Fix deterministic issues directly when safe.
3. Put contested or judgment-heavy items under `scratch/review/`.
4. Append `wiki/log.md`.

## Completion Gate

A wiki task is not complete until:

- raw files were not mutated,
- links and frontmatter validate,
- source drift is checked when source pages have hashes,
- index/log/report updates are present when required,
- human locks are preserved,
- `scripts/release_gate.sh` exits successfully.
