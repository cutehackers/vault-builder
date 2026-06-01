---
title: LLM-Maintained Wiki
type: concept
status: active
created: "2026-05-26"
updated: "2026-05-30"
owner: agent
summary: A markdown wiki maintained by agents under explicit provenance and quality rules.
source_count: 1
tags:
  - llm-wiki
  - agents
  - operations
related:
  - concepts/compiled-knowledge
  - workflows/ingest-new-source
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


# LLM-Maintained Wiki

## Definition

An LLM-maintained wiki is a persistent markdown knowledge base where agents perform summarization, cross-reference maintenance, contradiction detection, indexing, and log updates under human-controlled rules.

Source: Human instruction in `AGENTS.md`.

## Why It Matters

The value of the vault depends on compounding maintenance. Agents should not only answer questions; they should integrate durable answers back into the wiki when the answer will be useful later.

Source: Human instruction in `AGENTS.md`.

## Current Synthesis

The human owns judgment and source curation. Agents own bookkeeping, synthesis, and structural maintenance. Durable knowledge remains in markdown and Git, not in an agent's context window.

Status: inferred
Confidence: high
Rationale: This is the operating split described by the vault contract.

## Mechanism / Structure

- `raw/` preserves source material.
- `wiki/` stores compiled knowledge.
- `scratch/` stores reports, drafts, and review queues.
- `tools/wiki/` should enforce deterministic quality checks over time.

## Examples

- Updating related pages after a source ingest.
- Recording a contradiction instead of silently resolving it.
- Filing a reusable query answer into a concept or workflow page.

## Related Concepts

- [[concepts/compiled-knowledge]]
- [[concepts/source-of-truth]]

## Supporting Sources

- Human instruction in `AGENTS.md`.

## Open Questions

- Which agent-facing commands should be exposed after the local CLI stabilizes?

## Change Notes

- 2026-05-26 — Created during baseline repair for the quality-gated wiki architecture.

## Claim Evidence

| Claim | Status | Evidence |
|---|---|---|
| An LLM-maintained wiki uses agents for synthesis, indexing, contradiction detection, and log maintenance under human-controlled rules. | stated | Human instruction in `AGENTS.md`. |
| Durable knowledge remains in markdown and Git instead of an agent context window. | stated | Human instruction in `AGENTS.md`. |
