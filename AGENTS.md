# AGENTS.md - LLM Wiki Agent Contract

This vault is a quality-gated compiled markdown wiki.

Read this file first. Read the longer reference only when the task needs detail:

- Full operating schema: `docs/agent/OPERATING-SCHEMA.md`
- Architecture: `docs/LLM-WIKI.md`
- Skill workflows: `.agents/skills/`
- Draft guide: `docs/agent/DRAFTS.md`
- Human guide: `README.md`

## Output Style & Language Rules

- Be clear, concise, and direct. Match the user's input language.
- In Korean, keep identifiers and official technical terms in English, but do not attach Korean particles or verb endings directly to them. Use natural Korean syntax.

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
8. One Body Owner, Many References: canonical pages own explanations; source pages are registration/provenance manifests with links, never copied semantic bodies.
9. `vault-builder` owns contracts and explicit migration; agents never edit `.vault-builder/manifest.json` or run migration `apply` implicitly.

## Directory Map

| Path               | Meaning                                                                   |
| ------------------ | ------------------------------------------------------------------------- |
| `raw/`             | Immutable source material.                                                |
| `wiki/`            | Durable compiled knowledge.                                               |
| `scratch/drafts/`  | LLM proposals before validation.                                          |
| `scratch/reports/` | Lint, ingest, audit, query, and repair reports.                           |
| `scratch/review/`  | Human-judgment queue.                                                     |
| `tools/wiki/`      | Deterministic quality tooling.                                            |
| `.agents/skills/`  | Agent workflow guides for `wiki-ingest`, `wiki-update`, and `wiki-query`. |
| `docs/agent/`      | Detailed agent reference.                                                 |

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

For ingest, update, or query tasks, follow the matching guide under `.agents/skills/`, read `wiki/index.md` plus relevant pages or sources, preserve locks and provenance, and use drafts with `publish-draft` for durable changes. Use workflow checkpoints and write reports when applicable.

For lint or repair tasks, run `python3 tools/wiki/cli.py lint --report`, fix deterministic issues directly when safe, put judgment-heavy items under `scratch/review/`, and append `wiki/log.md`.

## Completion Gate

A wiki task is not complete until:

- raw files were not mutated,
- links and frontmatter validate,
- source drift is checked when source pages have hashes,
- index/log/report updates are present when required,
- human locks are preserved,
- `scripts/release_gate.sh` exits successfully.
