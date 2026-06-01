---
title: Use Markdown for Compiled Knowledge
type: decision
status: active
created: "2026-05-26"
updated: "2026-05-30"
owner: agent
summary: Decision to use Git-backed markdown files as the durable store for compiled wiki knowledge.
source_count: 1
tags:
  - decision
  - markdown
  - git
  - knowledge-store
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
decision_status: accepted
decided_at: "2026-05-26"
---


# Use Markdown for Compiled Knowledge

## Decision

Use Git-backed markdown files under `wiki/` as the durable compiled-knowledge store.

Source: Human instruction in `AGENTS.md`.

## Context

The vault needs to remain human-browsable, agent-maintainable, reviewable, and portable. Raw sources are immutable inputs, while wiki pages are compiled knowledge artifacts.

Source: Human instruction in `AGENTS.md`.

## Options Considered

| Option | Pros | Cons |
|---|---|---|
| Git-backed markdown | Human-readable, portable, diffable, Obsidian-compatible, easy for agents to edit | Requires linting discipline |
| Database-first store | Strong structure and querying | Harder for humans to inspect and review |
| RAG-only folder | Fast to start | Does not compound into durable human-browsable knowledge |

## Rationale

Markdown plus Git makes every durable change reviewable and keeps the wiki independent of any single agent, model, or context window.

Status: inferred
Confidence: high
Rationale: This follows the source-of-truth and compiled-knowledge rules in the vault contract.

## Consequences

- Agents must maintain links, indexes, provenance, and logs.
- Deterministic linting becomes important.
- Raw source evidence remains separate from synthesized knowledge.

## Revisit Conditions

Revisit this decision if the wiki outgrows markdown navigation, needs strict multi-user workflow controls, or requires database-backed query guarantees.

## Sources / Discussion

- Human instruction in `AGENTS.md`.

## Claim Evidence

| Claim | Status | Evidence |
|---|---|---|
| Git-backed markdown is the durable compiled-knowledge store for this vault. | stated | Human instruction in `AGENTS.md`. |
| Markdown keeps durable knowledge human-readable, portable, diffable, and agent-maintainable. | stated | Human instruction in `AGENTS.md`. |
