---
title: Overview
type: overview
status: active
created: "2026-05-26"
updated: "2026-05-31"
owner: agent
summary: Executive briefing for the LLM Wiki and its quality-gated operating model.
source_count: 1
tags:
  - overview
  - llm-wiki
  - quality
related:
  - index
  - maps/wiki-operating-map
  - workflows/ingest-new-source
confidence: high
quality:
  provenance: claim
  links: unchecked
  contradictions: "none"
  review_required: false
---

# Overview

## Scope

This vault is an LLM-managed compiled knowledge system. Raw source material lives under `raw/`, while durable synthesized knowledge lives under `wiki/`.

The wiki is designed to improve through ingestion, querying, linting, mapping, metrics, and explicit human review.

Source: Human instruction in `AGENTS.md`.

## Current Synthesis

The central operating model is a quality-gated compiled wiki:

1. Humans provide or reference source material.
2. LLM agents extract durable knowledge and propose updates.
3. Deterministic wiki tooling validates structure, links, wiki-root link boundaries, provenance, source hashes, review state, human locks, maps, and maintenance signals.
4. Durable knowledge is written to markdown and preserved by Git.

Status: inferred
Confidence: high
Rationale: This follows the vault contract in `AGENTS.md` and the approved improvement direction.

## Current Architecture

The canonical architecture definition is `docs/LLM-WIKI.md`. `AGENTS.md` is the concise agent entrypoint, `docs/agent/OPERATING-SCHEMA.md` is the detailed reference, and `docs/agent/DRAFTS.md` defines semantic draft handoff. The implemented tools support page validation, wiki linting, raw source hashing, deterministic source registration, transactional JSON draft publication, idempotent review-backed merge scans, generated maps, scratch-confined reports and query-capture drafts, configurable maintenance metrics, repo-local and CI release gating, and stdio MCP access.

## Major Areas

- [[concepts/compiled-knowledge]] - The core idea that wiki pages are compiled artifacts, not raw notes.
- [[concepts/llm-maintained-wiki]] - The agent-maintained operating style for this vault.
- [[concepts/source-of-truth]] - The boundary between immutable raw input and mutable compiled knowledge.
- [[workflows/ingest-new-source]] - The main workflow for turning raw input into grounded wiki pages.
- [[workflows/wiki-linting]] - The workflow for keeping the wiki healthy over time.
- [[maps/topic-map]] - Generated map of durable wiki pages by type and tag.
- [[maps/source-map]] - Generated map of raw source registration state.
- [[maps/review-map]] - Generated map of unresolved human review items.
- [[maps/lifecycle-map]] - Generated map of stale, contested, deprecated, and source-attention signals.

## Key Decisions

- [[decisions/use-markdown-for-compiled-knowledge]] - Markdown plus Git is the durable knowledge store.

## Open Questions

- What richer semantic extraction prompts should agents use before `publish-draft`?
- When should `scratch/review/` items be promoted into dedicated claim pages?
- Which operator-facing dashboards should be added for long-running vault maintenance?

## Recommended Reading Order

1. [[concepts/source-of-truth]]
2. [[concepts/compiled-knowledge]]
3. [[workflows/ingest-new-source]]
4. [[workflows/wiki-linting]]
5. [[maps/wiki-operating-map]]
6. [[maps/topic-map]]

## Claim Evidence

| Claim | Status | Evidence |
|---|---|---|
| This vault separates immutable raw source material under raw/ from durable synthesized knowledge under wiki/. | stated | Human instruction in `AGENTS.md`. |
| Durable wiki knowledge is validated by deterministic tooling before it is treated as permanent. | stated | Human instruction in `AGENTS.md`. |
| Maintenance metrics thresholds, scratch-confined artifacts, and CI release validation are now part of the operating surface. | stated | Human instruction in current task. |
