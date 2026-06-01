---
title: Wiki Index
type: index
status: active
created: "2026-05-26"
updated: "2026-05-31"
owner: agent
summary: Catalog of durable pages in the LLM Wiki.
source_count: 0
tags:
  - index
  - navigation
related:
  - overview
  - maps/wiki-operating-map
confidence: high
quality:
  provenance: "none"
  links: unchecked
  contradictions: "none"
  review_required: false
---

# Wiki Index

## Overview

- [[overview]] - High-level synthesis of the wiki.
- [[inbox]] - Temporary capture area for unprocessed notes.
- [[log]] - Chronological operation history.

## Architecture Documents

- `docs/LLM-WIKI.md` - Canonical architecture definition for the quality-gated compiled wiki.
- `docs/usage.md` - Human-facing usage guide for common workflows and commands.
- `docs/architecture.md` - Implementation-oriented architecture guide.
- `docs/agent/OPERATING-SCHEMA.md` - Full agent reference split out from the concise `AGENTS.md` entrypoint.
- `docs/agent/DRAFTS.md` - Semantic draft rules and the source-to-draft handoff contract.

## Sources

- None yet.

## Concepts

- [[concepts/compiled-knowledge]] - Persistent synthesized knowledge maintained across sources and queries.
- [[concepts/llm-maintained-wiki]] - A wiki maintained by LLM agents under explicit operating rules.
- [[concepts/source-of-truth]] - The rule that raw sources and Git-backed markdown preserve durable memory.

## Entities

- None yet.

## Systems

- None yet.

## Workflows

- [[workflows/ingest-new-source]] - The quality-gated workflow for turning raw sources into compiled wiki pages.
- [[workflows/wiki-linting]] - The workflow for checking schema, links, provenance, contradictions, and navigation health.

## Decisions

- [[decisions/use-markdown-for-compiled-knowledge]] - Decision to use Git-backed markdown as the durable compiled-knowledge store.

## Maps

- [[maps/wiki-operating-map]] - Reading path for the core operating model of this vault.
- [[maps/topic-map]] - Generated navigation map grouping durable wiki pages by type and tag.
- [[maps/source-map]] - Generated navigation map for raw source registration and source pages.
- [[maps/decision-map]] - Generated navigation map for durable decisions.
- [[maps/review-map]] - Generated navigation map for pending human-review items.
- [[maps/lifecycle-map]] - Generated navigation map for stale, contested, deprecated, and source-attention signals.
