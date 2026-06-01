---
title: Ingest New Source
type: workflow
status: active
created: "2026-05-26"
updated: "2026-05-31"
owner: agent
summary: Quality-gated workflow for turning immutable raw sources into durable wiki knowledge.
source_count: 1
tags:
  - ingest
  - workflow
  - quality-gate
related:
  - concepts/compiled-knowledge
  - concepts/source-of-truth
  - workflows/wiki-linting
confidence: high
quality:
  provenance: claim
  links: unchecked
  contradictions: "none"
  review_required: false
primary_sources: []
derived_from:
  - AGENTS.md
---

# Ingest New Source

## Goal

Turn a raw source into durable, navigable, provenance-backed wiki knowledge.

Source: Human instruction in `AGENTS.md`.

## Trigger

Run this workflow when the human adds a source under `raw/`, references a file, or asks to ingest a URL or document.

## Inputs

- Raw source file or URL.
- Existing `wiki/index.md`.
- Related pages found through search.
- Human instructions that constrain interpretation.

## Procedure

1. Identify the source and avoid mutating `raw/`.
2. Register the source with `python3 tools/wiki/cli.py ingest-source raw/sources/example.md --report`.
3. Extract durable concepts, entities, systems, workflows, decisions, claims, contradictions, and open questions into a JSON draft under `scratch/drafts/` using `tools/wiki/templates/draft-upsert-page.json`.
4. Compare the extraction with existing wiki pages to avoid duplicates.
5. Publish the draft with `python3 tools/wiki/cli.py publish-draft scratch/drafts/example.json --report`.
6. Run `python3 tools/wiki/cli.py lint --report`.
7. Move unresolved judgment items to `scratch/review/`.

## Outputs

- Source page.
- Source hash.
- Updated related wiki pages.
- Ingest report.
- Index update.
- Log entry.
- Optional review items.

## Quality Gates

- [ ] Raw source was not modified.
- [ ] Source page exists when the source is ingested.
- [ ] Source page records `canonical_source` and `raw_sha256`.
- [ ] Relevant existing pages were checked.
- [ ] New pages are not duplicates.
- [ ] Claims have provenance.
- [ ] Contradictions were surfaced.
- [ ] Index and log were updated.

## Failure Handling

If provenance, duplicate detection, or contradiction handling cannot be completed, do not silently publish the result as stable knowledge. Record the unresolved item in `scratch/review/` or `scratch/reports/`.

## Related Pages

- [[concepts/compiled-knowledge]]
- [[concepts/source-of-truth]]
- [[workflows/wiki-linting]]

## Claim Evidence

| Claim | Status | Evidence |
|---|---|---|
| Ingesting a source should register the raw source, create a semantic draft, publish through validation, and leave reports or review items when needed. | stated | Human instruction in `AGENTS.md`. |
| The ingest workflow must avoid mutating raw sources. | stated | Human instruction in `AGENTS.md`. |
