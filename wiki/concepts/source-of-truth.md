---
title: Source of Truth
type: concept
status: active
created: "2026-05-26"
updated: "2026-05-30"
owner: agent
summary: The boundary that keeps raw sources immutable and durable knowledge Git-backed.
source_count: 1
tags:
  - provenance
  - source-of-truth
  - git
related:
  - concepts/compiled-knowledge
  - workflows/ingest-new-source
  - decisions/use-markdown-for-compiled-knowledge
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


# Source of Truth

## Definition

The source of truth rule says that raw inputs remain immutable under `raw/`, while durable synthesized knowledge lives in Git-backed markdown under `wiki/`.

Source: Human instruction in `AGENTS.md`.

## Why It Matters

This boundary prevents agents from rewriting evidence while still allowing the wiki to improve. If a raw source is wrong, the correction belongs in the wiki with provenance, not in the raw file.

Source: Human instruction in `AGENTS.md`.

## Current Synthesis

The vault has two truth layers:

- Raw truth: the original files, assets, and imports.
- Compiled truth: reviewed markdown pages that cite raw sources or explicit human instructions.

Status: inferred
Confidence: high
Rationale: This directly follows the immutable raw source rule and compiled wiki model.

## Mechanism / Structure

1. Store primary material under `raw/`.
2. Preserve source hashes when possible.
3. Store synthesis under `wiki/`.
4. Use Git history to preserve every durable wiki change.
5. Record corrections and contradictions in wiki pages instead of mutating raw sources.

## Examples

- A PDF stays unchanged in `raw/sources/`.
- A source page records a SHA-256 hash for that PDF.
- A concept page cites the source page and specific raw evidence.

## Related Concepts

- [[concepts/compiled-knowledge]]
- [[decisions/use-markdown-for-compiled-knowledge]]

## Supporting Sources

- Human instruction in `AGENTS.md`.

## Open Questions

- Should source hashes be body-only or file-level for each source type?

## Change Notes

- 2026-05-26 — Created during baseline repair for the quality-gated wiki architecture.

## Claim Evidence

| Claim | Status | Evidence |
|---|---|---|
| Raw inputs remain immutable under raw/ while durable synthesized knowledge lives in Git-backed markdown under wiki/. | stated | Human instruction in `AGENTS.md`. |
| Corrections to raw-source errors belong in wiki pages with provenance, not by mutating raw files. | stated | Human instruction in `AGENTS.md`. |
