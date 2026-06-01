---
title: Wiki Linting
type: workflow
status: active
created: "2026-05-26"
updated: "2026-05-31"
owner: agent
summary: "Workflow for detecting structural, provenance, link, and navigation problems in the wiki."
source_count: 1
tags:
  - lint
  - quality
  - workflow
related:
  - concepts/compiled-knowledge
  - workflows/ingest-new-source
  - maps/wiki-operating-map
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

# Wiki Linting

## Goal

Keep the wiki structurally valid, navigable, provenance-backed, and safe to build on.

Source: Human instruction in `AGENTS.md`.

## Trigger

Run linting after meaningful wiki changes, before claiming a wiki operation is complete, and periodically during maintenance.

## Inputs

- All markdown files under `wiki/`.
- `wiki/index.md`.
- `wiki/log.md`.
- Raw source references.
- Human-lock markers.
- Review queue items under `scratch/review/`.

## Procedure

1. Check required frontmatter.
2. Check broken wiki links and reject links that escape the `wiki/` root.
3. Check index entries against existing pages.
4. Check source pages, `canonical_source`, and `raw_sha256` drift.
5. Flag pages with unsupported claims.
6. Flag unresolved contradictions or review items.
7. Check review item related pages and raw evidence references.
8. Write a lint report under `scratch/reports/`.
9. Route deterministic fixes through draft/publish or a targeted tool; route judgment-heavy items to review.

## Outputs

- Lint report.
- Human-review list for contested or judgment-heavy issues.
- Follow-up draft or targeted tool run when a deterministic repair is safe.

## Quality Gates

- [ ] Every durable page has required frontmatter.
- [ ] Important pages are reachable from the index or a map.
- [ ] Broken links and wiki-root escape links are reported or repaired.
- [ ] Claims without provenance are reported.
- [ ] Source hash drift is reported.
- [ ] Review queue references still resolve.
- [ ] Human-locked content is preserved.
- [ ] Deletions, major merges, and contested claim changes require human approval.

## Failure Handling

If linting cannot determine whether a claim is supported, mark the issue for review instead of rewriting the claim.

## Related Pages

- [[concepts/compiled-knowledge]]
- [[workflows/ingest-new-source]]
- [[maps/wiki-operating-map]]

## Claim Evidence

| Claim | Status | Evidence |
|---|---|---|
| Wiki linting checks frontmatter, links, navigation, provenance, source drift, log shape, review references, and human locks. | stated | Human instruction in `AGENTS.md`. |
| Wiki links must resolve inside the wiki root and must not escape to sibling files. | stated | Human instruction in current task. |
| Unclear support or contested judgment should be routed to review instead of silently rewritten. | stated | Human instruction in `AGENTS.md`. |
