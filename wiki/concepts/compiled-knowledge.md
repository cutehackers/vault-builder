---
title: Compiled Knowledge
type: concept
status: active
created: "2026-05-26"
updated: "2026-05-30"
owner: agent
summary: "Durable synthesized knowledge produced from sources, queries, and review."
source_count: 1
tags:
  - llm-wiki
  - knowledge-quality
related:
  - concepts/llm-maintained-wiki
  - concepts/source-of-truth
  - workflows/ingest-new-source
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


# Compiled Knowledge

## Definition

Compiled knowledge is durable synthesis extracted from raw sources, human instructions, and useful query results. It is organized into wiki pages with provenance, links, review state, and change history.

Source: Human instruction in `AGENTS.md`.

## Why It Matters

Compiled knowledge prevents the vault from becoming a passive pile of retrieved chunks. The goal is for each source or useful answer to improve the persistent wiki.

Source: Human instruction in `AGENTS.md`.

## Current Synthesis

The wiki should treat raw sources as immutable inputs and wiki pages as mutable compiled artifacts. LLM agents may propose meaning, but durable updates need provenance, navigation, contradiction handling, and log entries.

Status: inferred
Confidence: high
Rationale: This follows the operating contract and the approved quality-gated improvement direction.

## Mechanism / Structure

1. Raw material enters `raw/`.
2. A semantic draft identifies concepts, entities, claims, contradictions, and open questions.
3. A quality workflow checks the draft against existing pages and provenance rules.
4. The wiki receives only the durable synthesis.
5. Indexes, backlinks, and logs are updated.

Source: Human instruction in `AGENTS.md`.

## Examples

- A source page summarizing one raw document.
- A concept page merging repeated ideas across several sources.
- A decision page recording why a knowledge-management rule was adopted.

## Related Concepts

- [[concepts/llm-maintained-wiki]]
- [[concepts/source-of-truth]]

## Supporting Sources

- Human instruction in `AGENTS.md`.

## Open Questions

- Which quality metrics should become deterministic checks first?

## Change Notes

- 2026-05-26 — Created during baseline repair for the quality-gated wiki architecture.

## Claim Evidence

| Claim | Status | Evidence |
|---|---|---|
| Compiled knowledge is durable synthesis extracted from raw sources, human instructions, and useful query results. | stated | Human instruction in `AGENTS.md`. |
| Compiled knowledge prevents the vault from becoming a passive pile of retrieved chunks. | stated | Human instruction in `AGENTS.md`. |
