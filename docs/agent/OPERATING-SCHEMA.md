# LLM Wiki Operating Schema

> Purpose: This file is the full reference manual for agents. `AGENTS.md` is the short operational entrypoint; this document keeps the detailed schema, templates, and workflow rules.

---

## 0. Core Principle

This is not a passive RAG folder.

The goal is to maintain a persistent, compounding, interlinked markdown wiki that improves every time a source is ingested or a useful question is answered.

The agent must not merely retrieve chunks at query time. The agent must:

1. Read raw sources.
2. Extract durable knowledge.
3. Integrate that knowledge into existing wiki pages.
4. Update links, indexes, summaries, contradictions, and logs.
5. Preserve provenance back to source material.
6. Keep the wiki navigable by humans and useful to future agents.

Human role:

- Curate sources.
- Ask questions.
- Review important changes.
- Make final judgment on ambiguous or contested claims.

Agent role:

- Summarize.
- Cross-reference.
- Maintain structure.
- Detect contradictions.
- Update pages.
- Keep indexes and logs current.

---

## 1. Vault Topology (Directory Structure)

Use the default topology below unless the human explicitly asks for a specialized structure.

The default topology is intentionally compact. The human should not need to decide where most wiki pages go; the agent owns that decision.

```text
.
├── AGENTS.md
├── raw/
│   ├── sources/
│   ├── assets/
│   └── imports/
├── wiki/
│   ├── index.md
│   ├── log.md
│   ├── inbox.md
│   ├── overview.md
│   ├── sources/
│   ├── concepts/
│   ├── entities/
│   ├── systems/
│   ├── workflows/
│   ├── decisions/
│   └── maps/
├── scratch/
│   ├── drafts/
│   ├── reports/
│   └── review/
└── tools/
    └── wiki/
```

### 1.1 Human Usage Contract

The human normally does only three things:

1. Put source material under `raw/sources/`, `raw/imports/`, or provide a URL/file reference.
2. Ask the agent to ingest, query, lint, or map the wiki.
3. Browse `wiki/overview.md`, `wiki/index.md`, and `wiki/maps/`.

The human should not have to manually maintain backlinks, index entries, page placement, source hashes, or contradiction records.

### 1.2 Directory Semantics

| Path | Owner | Mutability | Purpose |
|---|---:|---:|---|
| `raw/` | Human | Immutable | Source of truth. The agent may read but must not edit. |
| `raw/sources/` | Human | Immutable | Stable primary sources: notes, PDFs, articles, transcripts, exports, papers. |
| `raw/assets/` | Human/Agent | Immutable after capture | Images, screenshots, diagrams, and attachments referenced by sources. |
| `raw/imports/` | Human/Agent | Immutable after capture | Temporary landing zone for newly clipped or imported files before ingest. |
| `wiki/` | Agent | Mutable | Compiled knowledge. The agent creates and updates pages here. |
| `wiki/index.md` | Agent | Mutable | Content catalog. Read first before answering or updating. |
| `wiki/log.md` | Agent | Append-only | Chronological operation history. |
| `wiki/inbox.md` | Human/Agent | Mutable | Unprocessed notes, loose ideas, and temporary capture. |
| `wiki/overview.md` | Agent | Mutable | Executive-level synthesis of the whole wiki. |
| `wiki/sources/` | Agent | Mutable | One page per ingested source. |
| `wiki/concepts/` | Agent | Mutable | Durable ideas, principles, patterns, terms, and abstractions. |
| `wiki/entities/` | Agent | Mutable | People, organizations, products, repositories, projects, tools, named artifacts. |
| `wiki/systems/` | Agent | Mutable | Apps, architectures, services, agents, workflows engines, technical systems. |
| `wiki/workflows/` | Agent | Mutable | Repeatable processes the human or agent follows. |
| `wiki/decisions/` | Agent | Mutable | Architectural decisions and knowledge-management decisions. |
| `wiki/maps/` | Agent | Mutable | Reading paths and navigational maps for topic clusters. |
| `scratch/drafts/` | Agent | Ephemeral | LLM-generated semantic drafts before deterministic validation. Never cite as durable source truth. |
| `scratch/reports/` | Agent | Mutable | Ingest reports, query reports, lint reports, audit reports, proposed patches. |
| `scratch/review/` | Human/Agent | Ephemeral | Pending human-judgment items for contested claims, merges, and unresolved decisions. |
| `tools/` | Human/Agent | Mutable with care | Optional helper scripts for search, linting, hashing, validation, and automation. |
| `tools/wiki/` | Agent | Mutable with care | Deterministic wiki quality tooling: validation, linting, hashing, draft application, workflow checkpoints, maps, metrics, and MCP wrappers. |
| `AGENTS.md` | Human + Agent | Controlled | Operating schema. Update only with explicit or strongly implied approval. |

### 1.3 Expansion Rule

Do not create extra top-level folders just because a new page type appears.

Create an optional folder only when at least one of these is true:

- The category has 5+ durable pages.
- The category is central to the domain.
- The existing folders are causing repeated misclassification.
- The human explicitly asks for the separation.

Optional expansion folders:

```text
wiki/claims/        # high-impact or contested claims
wiki/comparisons/   # repeated side-by-side analyses
wiki/timelines/     # long chronological histories
wiki/questions/     # durable research questions
wiki/glossary/      # many short definitions; otherwise use wiki/glossary.md
wiki/specs/         # software specification pages
wiki/incidents/     # postmortems, failure reports, production incidents
wiki/experiments/   # experiments, evaluations, benchmark runs
```

When creating an optional folder, append a `schema-change` entry to `wiki/log.md` explaining why.

---

## 2. Non-Negotiable Rules

### 2.1 Raw Sources Are Immutable

Never edit, rewrite, rename, or delete files under `raw/` unless explicitly instructed.

If a raw source contains errors, record corrections in the wiki, not in the source.

### 2.2 Wiki Pages Must Be Grounded

Every substantive claim in the wiki should be traceable to one or more sources, prior wiki pages, or an explicit human instruction.

Use one of these provenance formats:

```markdown
Source: [[sources/source-title]]
Evidence: `raw/sources/source-file.md`, section "..."
```

or:

```markdown
<!-- provenance: raw/sources/source-file.md#section-name -->
```

For claims that are inferred rather than directly stated, mark them explicitly:

```markdown
Status: inferred
Confidence: medium
Rationale: ...
```

### 2.3 Do Not Silently Resolve Contradictions

When sources disagree, do not pick a winner silently.

Instead:

1. Record both positions.
2. Cite the supporting sources.
3. Mark the claim or section as `contested`.
4. Explain whether the conflict is factual, terminological, contextual, or caused by source age.
5. Ask for human judgment when the contradiction affects important decisions.

If the contradiction is local, record it in the relevant page.

If the contradiction is high-impact or recurring, create `wiki/claims/` and add a dedicated claim page.

### 2.4 Preserve Human Edits

If a page contains this marker, do not overwrite it without explicit approval:

```markdown
<!-- human-locked -->
```

If a section contains this marker, treat only that section as locked:

```markdown
<!-- human-locked:start -->
...
<!-- human-locked:end -->
```

When updating a locked page, append a proposed patch to `scratch/reports/` instead of editing the page directly.

### 2.5 Prefer Incremental Updates Over Replacement

When new information arrives, update the relevant existing pages instead of creating duplicate pages.

Create a new page only when:

- A durable concept/entity/system/workflow/decision deserves its own page.
- The topic is referenced from at least two places or is likely to recur.
- The page will improve navigation or future synthesis.

### 2.6 Keep the Wiki Human-Browsable

Every important page should contain:

- A clear title.
- A short summary.
- Links to related pages.
- Source references.
- Updated metadata.

Avoid pages that are only useful to an embedding system.

---

## 3. Naming Conventions

Use lowercase kebab-case filenames.

```text
wiki/concepts/compiled-knowledge.md
wiki/systems/rail-harness.md
wiki/workflows/source-ingestion.md
wiki/decisions/use-markdown-as-wiki-store.md
```

Avoid generic names:

```text
bad: notes.md
bad: misc.md
bad: stuff.md
bad: summary.md
bad: temp.md
```

Prefer specific names:

```text
good: codex-harness-observability.md
good: llm-wiki-ingestion-loop.md
good: specification-drift.md
```

### 3.1 Page Placement Rules

| Page Type | Default Location | Example |
|---|---|---|
| `source` | `wiki/sources/` | `karpathy-llm-wiki-gist.md` |
| `concept` | `wiki/concepts/` | `compiled-knowledge.md` |
| `entity` | `wiki/entities/` | `openai-codex.md` |
| `system` | `wiki/systems/` | `rail-harness.md` |
| `workflow` | `wiki/workflows/` | `ingest-new-source.md` |
| `decision` | `wiki/decisions/` | `use-obsidian-as-wiki-ide.md` |
| `map` | `wiki/maps/` | `llm-wiki-operating-map.md` |

Optional page types may live in the nearest default folder until they become numerous enough to deserve their own folder.

| Optional Type | Start Here | Promote To Folder When Needed |
|---|---|---|
| `claim` | Relevant concept/entity/system page | `wiki/claims/` |
| `comparison` | `wiki/concepts/` | `wiki/comparisons/` |
| `timeline` | Relevant entity/system page | `wiki/timelines/` |
| `question` | `wiki/inbox.md` or relevant page | `wiki/questions/` |
| `glossary` | `wiki/glossary.md` | `wiki/glossary/` |

---

## 4. Required Frontmatter

Every wiki page must start with YAML frontmatter.

```yaml
title: "Human-readable title"
type: source | concept | entity | system | workflow | decision | map | claim | comparison | timeline | question | glossary | index | log | inbox | overview
status: seed | active | stable | contested | deprecated
created: YYYY-MM-DD
updated: YYYY-MM-DD
owner: agent
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

### 4.1 Field Definitions

| Field | Meaning |
|---|---|
| `title` | Human-readable page title. |
| `type` | Page category. Must match the taxonomy. |
| `status` | Maturity or reliability of the page. |
| `created` | First creation date. |
| `updated` | Last meaningful update date. |
| `owner` | Usually `agent`; use `human` only for explicitly human-authored pages. |
| `summary` | One-sentence indexable summary for navigation and search. |
| `source_count` | Number of primary sources directly supporting the page. |
| `tags` | Broad retrieval and browsing tags. |
| `related` | Important wiki links. |
| `confidence` | Overall synthesis confidence for the page, not claim-level certainty. |
| `quality.provenance` | Highest provenance granularity currently present: `none`, `partial`, `section`, or `claim`. |
| `quality.links` | Link health state from deterministic validation. |
| `quality.contradictions` | Whether contradictions are unchecked, absent, present, or contested. |
| `quality.review_required` | Whether a human judgment item is currently required. |

Optional fields:

```yaml
aliases: []
primary_sources: []
derived_from: []
supersedes: []
superseded_by: []
review_after: YYYY-MM-DD
canonical_source: "raw/..."
raw_sha256: "..."
```

### 4.2 Quality Gate Rule

Wiki pages are compiled knowledge artifacts, not raw LLM output.

LLM-generated drafts may propose summaries, claims, links, and page updates, but they are not durable knowledge until the wiki quality workflow validates and applies them.

A durable wiki update is incomplete until:

- Every substantive claim has page, section, or claim-level provenance.
- New pages were checked against existing pages for duplicate concepts.
- Related pages and backlinks were considered.
- `wiki/index.md` was updated when navigation changed.
- `wiki/log.md` was appended.
- Contradictions were recorded instead of erased.
- Human-review items were moved to `scratch/review/` when judgment is required.
- Non-trivial ingest, repair, or audit work produced a report in `scratch/reports/`.
- The repo-level completion gate `scripts/release_gate.sh` succeeds before reporting that durable state is final.

Quality fields in frontmatter must be treated as validation state. Agents may propose them in drafts, but deterministic tooling or explicit manual review must verify them before marking pages as valid.

### 4.3 Frontmatter Boundaries

Frontmatter is for routing, linting, indexing, and quality triage.

Do not turn frontmatter into the knowledge body. Keep these in markdown sections, tables, or inline provenance comments:

- Claim-by-claim evidence.
- Contradiction explanations and competing positions.
- Rationale for confidence.
- Decision options, tradeoffs, and consequences.
- Source takeaways and extracted concepts.
- Follow-up questions.
- Lint timestamps and detailed broken-link lists.

`source_count` is lintable metadata, not evidence by itself. Prefer `primary_sources: []` for page-level source inventory and claim tables for claim-level evidence.

---

## 5. Page Type Schemas

All page type templates inherit the required frontmatter from Section 4. If a template is copied, include `summary`, `source_count`, `tags`, `related`, `confidence`, and the `quality` block unless a more specific type rule says otherwise.

### 5.1 Source Page

Location: `wiki/sources/`

Purpose: One page per ingested source.

Template:

```markdown
---
title: "Source Title"
type: source
status: active
created: YYYY-MM-DD
updated: YYYY-MM-DD
owner: agent
summary: "One-sentence summary of the source."
source_count: 1
tags: []
related: []
confidence: high
quality:
  provenance: claim
  links: unchecked
  contradictions: unchecked
  review_required: false
canonical_source: "raw/..."
source_kind: markdown | pdf | web | transcript | image | code | other
source_date: YYYY-MM-DD
authors: []
url: ""
raw_sha256: "..."
---

# Source Title

## Summary

Short summary of the source in 5-10 lines.

## Key Takeaways

- ...
- ...

## Extracted Concepts

- [[concepts/concept-name]] — why it matters.

## Extracted Entities

- [[entities/entity-name]] — role in the source.

## Important Claims

| Claim | Status | Evidence |
|---|---|---|
| ... | stated | raw/... |

## Contradictions / Tensions

- None found.

## Pages Updated During Ingest

- [[...]]

## Follow-up Questions

- ...
```

### 5.2 Concept Page

Location: `wiki/concepts/`

Purpose: Durable idea, pattern, abstraction, term, or principle.

Template:

```markdown
---
title: "Concept Name"
type: concept
status: active
created: YYYY-MM-DD
updated: YYYY-MM-DD
owner: agent
summary: "One-sentence summary of the concept."
source_count: 0
tags: []
related: []
confidence: medium
quality:
  provenance: none | partial | section | claim
  links: unchecked
  contradictions: unchecked
  review_required: false
primary_sources: []
derived_from: []
---

# Concept Name

## Definition

Clear definition in plain language.

## Why It Matters

Explain its importance in this wiki's domain.

## Current Synthesis

The best current understanding across sources.

## Mechanism / Structure

How it works, broken down step by step.

## Examples

- Example 1.
- Example 2.

## Related Concepts

- [[...]]

## Supporting Sources

- [[sources/...]]

## Open Questions

- ...

## Change Notes

- YYYY-MM-DD — Created/updated because ...
```

### 5.3 Entity Page

Location: `wiki/entities/`

Purpose: Person, organization, product, project, repository, tool, or named artifact.

Template:

```markdown
---
title: "Entity Name"
type: entity
status: active
created: YYYY-MM-DD
updated: YYYY-MM-DD
owner: agent
summary: "One-sentence summary of the entity."
source_count: 0
tags: []
related: []
confidence: medium
quality:
  provenance: none | partial | section | claim
  links: unchecked
  contradictions: unchecked
  review_required: false
primary_sources: []
derived_from: []
aliases: []
---

# Entity Name

## Summary

Concise explanation of what this entity is.

## Role in This Wiki

Why this entity matters here.

## Key Facts

| Fact | Status | Source |
|---|---|---|
| ... | stated | [[sources/...]] |

## Relationships

- Related to [[...]] because ...

## Timeline

- YYYY-MM-DD — ...

## Open Questions

- ...
```

### 5.4 System Page

Location: `wiki/systems/`

Purpose: Architecture, app, service, agent, workflow engine, or technical system.

Template:

```markdown
---
title: "System Name"
type: system
status: active
created: YYYY-MM-DD
updated: YYYY-MM-DD
owner: agent
summary: "One-sentence summary of the system."
source_count: 0
tags: []
related: []
confidence: medium
quality:
  provenance: none | partial | section | claim
  links: unchecked
  contradictions: unchecked
  review_required: false
primary_sources: []
derived_from: []
---

# System Name

## Purpose

What the system exists to do.

## Context

Where it fits in the broader project.

## Architecture

Describe components and relationships.

## Data / Knowledge Flow

Step-by-step flow of information.

## Operational Rules

Important invariants and constraints.

## Dependencies

- [[...]]

## Risks / Failure Modes

- ...

## Decisions Affecting This System

- [[decisions/...]]

## Sources

- [[sources/...]]
```

### 5.5 Workflow Page

Location: `wiki/workflows/`

Purpose: Repeatable process the human or agent follows.

Template:

```markdown
---
title: "Workflow Name"
type: workflow
status: active
created: YYYY-MM-DD
updated: YYYY-MM-DD
owner: agent
summary: "One-sentence summary of the workflow."
source_count: 0
tags: []
related: []
confidence: medium
quality:
  provenance: none | partial | section | claim
  links: unchecked
  contradictions: unchecked
  review_required: false
primary_sources: []
derived_from: []
---

# Workflow Name

## Goal

What this workflow accomplishes.

## Trigger

When to run it.

## Inputs

- ...

## Procedure

1. ...
2. ...
3. ...

## Outputs

- ...

## Quality Gates

- [ ] ...
- [ ] ...

## Failure Handling

What to do when the workflow cannot complete.

## Related Pages

- [[...]]
```

### 5.6 Decision Page

Location: `wiki/decisions/`

Purpose: Architectural decision record or knowledge-management decision.

Template:

```markdown
---
title: "Decision Title"
type: decision
status: active
created: YYYY-MM-DD
updated: YYYY-MM-DD
owner: agent
summary: "One-sentence summary of the decision."
source_count: 0
tags: []
related: []
confidence: high
quality:
  provenance: none | partial | section | claim
  links: unchecked
  contradictions: unchecked
  review_required: false
primary_sources: []
derived_from: []
decision_status: proposed | accepted | superseded | rejected
decided_at: YYYY-MM-DD
---

# Decision Title

## Decision

State the decision clearly.

## Context

Why the decision was needed.

## Options Considered

| Option | Pros | Cons |
|---|---|---|
| ... | ... | ... |

## Rationale

Why this option was selected.

## Consequences

Positive and negative implications.

## Revisit Conditions

When this decision should be reconsidered.

## Sources / Discussion

- [[sources/...]]
```

### 5.7 Map Page

Location: `wiki/maps/`

Purpose: Navigation page for a topic cluster.

Template:

```markdown
---
title: "Map Name"
type: map
status: active
created: YYYY-MM-DD
updated: YYYY-MM-DD
owner: agent
summary: "One-sentence summary of the map."
source_count: 0
tags: []
related: []
confidence: medium
quality:
  provenance: none | partial | section | claim
  links: unchecked
  contradictions: unchecked
  review_required: false
primary_sources: []
derived_from: []
---

# Map Name

## Purpose

What this map helps the reader understand.

## Core Pages

- [[...]] — why to read it.

## Reading Path

1. [[...]]
2. [[...]]
3. [[...]]

## Related Concepts

- [[...]]

## Open Questions

- ...
```

### 5.8 Claim Page

Location: Use the relevant concept/entity/system page first. Create `wiki/claims/` only for important, contested, or recurring claims.

Purpose: Track important, contested, or high-impact claims.

Template:

```markdown
---
title: "Claim Title"
type: claim
status: active | contested | deprecated
created: YYYY-MM-DD
updated: YYYY-MM-DD
owner: agent
summary: "One-sentence summary of the claim."
source_count: 0
tags: []
related: []
confidence: low | medium | high
quality:
  provenance: claim
  links: unchecked
  contradictions: unchecked | none | present | contested
  review_required: false
claim_status: stated | inferred | contested | deprecated | speculative
evidence_level: weak | medium | strong
primary_sources: []
---

# Claim Title

## Claim

State the claim precisely.

## Current Status

`active`, `contested`, or `deprecated`.

## Evidence Supporting

| Evidence | Source | Strength |
|---|---|---|
| ... | [[sources/...]] | strong/medium/weak |

## Evidence Against / Tensions

| Evidence | Source | Strength |
|---|---|---|
| ... | [[sources/...]] | strong/medium/weak |

## Current Interpretation

How the wiki currently treats this claim.

## Needed Resolution

What would resolve the uncertainty.
```

---

## 6. Index and Log Rules

### 6.1 `wiki/index.md`

The index is content-oriented. Update it after every meaningful wiki change.

Required sections:

```markdown
# Wiki Index

## Overview
- [[overview]] — High-level synthesis of the wiki.

## Sources
- [[sources/...]] — One-line summary.

## Concepts
- [[concepts/...]] — One-line summary.

## Entities
- [[entities/...]] — One-line summary.

## Systems
- [[systems/...]] — One-line summary.

## Workflows
- [[workflows/...]] — One-line summary.

## Decisions
- [[decisions/...]] — One-line summary.

## Maps
- [[maps/...]] — One-line summary.

## Optional Sections
Add only if relevant: Claims, Comparisons, Timelines, Questions, Glossary.
```

Each entry should be concise:

```markdown
- [[concepts/compiled-knowledge]] — Persistent synthesized knowledge maintained across sources and queries.
```

### 6.2 `wiki/log.md`

The log is chronological and append-only.

Every entry must use this parseable prefix:

```markdown
## [YYYY-MM-DD] event-type | Short Title
```

Allowed event types:

- `ingest`
- `query`
- `lint`
- `repair`
- `decision`
- `schema-change`
- `manual-note`

Example:

```markdown
## [2026-05-22] ingest | Karpathy LLM Wiki Gist

- Added source page: [[sources/karpathy-llm-wiki-gist]]
- Updated concepts: [[concepts/compiled-knowledge]], [[concepts/wiki-as-codebase]]
- Added decision: [[decisions/use-markdown-as-compiled-knowledge-store]]
- Contradictions found: none
- Follow-ups: define local lint workflow
```

---

## 7. Ingestion Workflow

Run this workflow when the human adds or references a new source.

### 7.1 Input

A source may be:

- Markdown file.
- PDF.
- Web article.
- Transcript.
- Meeting note.
- Screenshot or image.
- Chat export.
- Research paper.
- Code-related design/spec document.

### 7.2 Procedure

1. **Identify the source**
   - Locate the raw file or URL.
   - If the source is not yet stored under `raw/`, suggest or create a raw copy when possible.
   - Do not mutate original raw files.

2. **Create source metadata**
   - Determine title, author, date, URL/path, and source type.
   - Compute hash if tooling exists.
   - Create or update a source page in `wiki/sources/`.

3. **Read for durable knowledge**
   Extract:
   - Key concepts.
   - Entities.
   - Systems.
   - Workflows.
   - Decisions.
   - Claims.
   - Contradictions.
   - Open questions.

4. **Compare against existing wiki**
   - Read `wiki/index.md` first.
   - Search for related pages.
   - Determine whether to create new pages or update existing ones.

5. **Prepare an ingest report**
   Save to:

   ```text
   scratch/reports/YYYY-MM-DD-ingest-source-slug.md
   ```

   Include:
   - New pages proposed.
   - Existing pages to update.
   - Contradictions found.
   - Claims requiring human review.
   - Confidence assessment.

6. **Apply changes**
   Unless the human requested report-only mode:
   - Create/update source page.
   - Update concept/entity/system/workflow/decision pages.
   - Update relevant claims or contradiction sections.
   - Update `wiki/index.md`.
   - Append `wiki/log.md`.

7. **Final response to human**
   Report:
   - What was ingested.
   - Which pages changed.
   - Any contradictions or uncertainties.
   - Suggested next question or action.

### 7.3 Ingestion Quality Gate

Before finishing ingestion, verify:

- [ ] Raw source was not modified.
- [ ] Source page exists.
- [ ] Relevant existing pages were checked.
- [ ] New pages are not duplicates.
- [ ] Index was updated.
- [ ] Log was appended.
- [ ] Contradictions were surfaced.
- [ ] Claims have provenance.
- [ ] Human-locked sections were preserved.

---

## 8. Query Workflow

Run this workflow when the human asks a question against the wiki.

### 8.1 Procedure

1. Read `wiki/index.md`.
2. Identify candidate pages.
3. Read relevant pages.
4. If needed, inspect source pages or raw sources.
5. Answer with synthesis, not merely page summaries.
6. Cite wiki pages and raw sources where possible.
7. If the answer creates durable value, propose filing it back into the wiki.

### 8.2 Filing Query Outputs Back Into the Wiki

A query result should become a wiki page when it is:

- A reusable comparison.
- A durable explanation.
- A new decision.
- A synthesis across multiple sources.
- A workflow the human will reuse.
- A map of related concepts.

Save query reports to:

```text
scratch/reports/YYYY-MM-DD-query-slug.md
```

If promoted into the wiki, create the appropriate page and log it.

---

## 9. Lint Workflow

Run periodically or when the human asks to “clean”, “lint”, “repair”, or “health-check” the wiki.

### 9.1 Lint Checks

Check for:

- Orphan pages with no inbound links.
- Broken wiki links.
- Duplicate pages.
- Stale pages with old `updated` dates.
- Missing frontmatter.
- Missing source references.
- Claims without provenance.
- Contradictions between pages.
- Pages with unclear status.
- Important concepts mentioned repeatedly but lacking pages.
- Source pages not listed in `index.md`.
- Pages listed in `index.md` that no longer exist.
- Human-locked pages that have proposed but unapplied updates.
- Optional folders that were created prematurely and contain too few pages.
- Generated navigation maps that are stale relative to current wiki state.
- Report paths or generated query-capture draft paths that escape `scratch/reports/` or `scratch/drafts/`.

The full release gate also runs tests, health, `maps build --check`, `metrics --check`, optional Stenc validation/render checks when `WIKI_ENABLE_STENC=1`, and `git diff --check` in a temporary repo copy.

### 9.2 Lint Report

Save to:

```text
scratch/reports/YYYY-MM-DD-lint.md
```

Template:

```markdown
# Wiki Lint Report — YYYY-MM-DD

## Summary

- Pages checked: N
- Issues found: N
- Auto-fixable: N
- Requires human review: N

## Broken Links

- ...

## Orphan Pages

- ...

## Duplicate / Overlapping Pages

- ...

## Contradictions

- ...

## Stale Pages

- ...

## Topology Issues

- ...

## Proposed Fixes

- ...

## Applied Fixes

- ...
```

### 9.3 Auto-Fix Policy

The agent may automatically fix:

- Broken index entries.
- Missing backlinks.
- Minor formatting problems.
- Missing frontmatter fields when values are obvious.
- Duplicate tags.
- Stale `updated` dates after actual content edits.

The agent must ask before fixing:

- Contested claims.
- Human-locked content.
- Major page merges.
- Deleting pages.
- Changing decisions.
- Reclassifying important concepts.
- Creating new optional topology folders.

---

## 10. Linking Rules

Use Obsidian-style links:

```markdown
[[concepts/compiled-knowledge]]
[[systems/rail-harness]]
[[decisions/use-git-for-wiki-versioning]]
```

Prefer direct links with path when ambiguity is possible.

### 10.1 Link Density

A page should link to:

- Parent concepts.
- Related concepts.
- Supporting sources.
- Relevant systems, workflows, decisions, or maps.

Do not overlink every repeated word. Link the first meaningful occurrence in a section.

### 10.2 Backlink Maintenance

When adding a page, update related existing pages with a backlink when the relationship is durable.

Example:

```markdown
## Related Concepts

- [[concepts/compiled-knowledge]] — This workflow turns raw sources into compiled knowledge.
```

---

## 11. Claims, Confidence, and Provenance

### 11.1 Claim Status

Use these statuses:

| Status | Meaning |
|---|---|
| `stated` | Directly stated by a source. |
| `inferred` | Reasonable synthesis from sources. |
| `contested` | Sources disagree or evidence is insufficient. |
| `deprecated` | Superseded by newer evidence or decision. |
| `speculative` | Plausible but not well-supported. |

### 11.2 Confidence Levels

| Level | Meaning |
|---|---|
| `high` | Multiple reliable sources or direct human instruction. |
| `medium` | One good source or strong inference. |
| `low` | Weak evidence, ambiguous source, or incomplete context. |

### 11.3 Required Wording for Uncertainty

Use explicit language:

```markdown
This appears to imply...
The current sources suggest...
This is contested because...
The wiki does not yet contain enough evidence to conclude...
```

Do not write uncertain claims as facts.

---

## 12. Contradiction Handling

When a new source conflicts with existing wiki content:

1. Identify the exact conflicting statements.
2. Quote or summarize the relevant evidence.
3. Determine whether the conflict is:
   - factual contradiction,
   - terminology mismatch,
   - newer source superseding older source,
   - different scope/context,
   - human preference change.
4. Update the relevant page with a `Contradictions / Tensions` section.
5. Create a dedicated claim page only if the contradiction is important or recurring.
6. Log the contradiction.
7. Ask for human judgment if needed.

Never erase the older view without recording why it changed.

---

## 13. Overview Page

`wiki/overview.md` is the top-level synthesis.

It should answer:

- What is this wiki about?
- What are the major systems/concepts/entities?
- What are the current strategic conclusions?
- What open questions matter most?
- What should a new agent read first?

Keep it concise. It is not an index. It is an executive briefing.

Required structure:

```markdown
# Overview

## Scope

## Current Synthesis

## Major Areas

## Key Decisions

## Open Questions

## Recommended Reading Order
```

---

## 14. Inbox Rules

Use `wiki/inbox.md` for unprocessed notes, loose observations, and temporary capture.

Each inbox item should follow:

```markdown
## [YYYY-MM-DD] Short Title

Status: unprocessed | processed | deferred
Source: ...
Note: ...
Next action: ...
```

During lint, process or defer inbox items.

Do not let `inbox.md` become a permanent dumping ground.

---

## 15. Maps

Use `wiki/maps/` for navigational pages that organize clusters of knowledge.

Examples:

```text
wiki/maps/agent-harness-map.md
wiki/maps/llm-wiki-map.md
wiki/maps/spec-driven-development-map.md
```

A map page should contain:

```markdown
# Map Name

## Purpose

## Core Pages

## Reading Path

1. [[...]]
2. [[...]]
3. [[...]]

## Related Concepts

## Open Questions
```

---

## 16. Source Hashing and Drift Detection

When possible, compute a body-only SHA-256 hash for raw sources and store it in the source page frontmatter.

```yaml
raw_sha256: "..."
```

During lint:

- Recompute hashes if tooling exists.
- Flag changed raw sources.
- Do not assume changed raw files are harmless.
- Create a lint issue if source drift is detected.

---

## 17. Git Discipline

Treat the wiki as a codebase.

Recommended commit types:

```text
ingest: add karpathy llm wiki source
wiki: update compiled knowledge concept
lint: repair broken backlinks
decision: record markdown store decision
schema: update vault topology rules
```

Before major changes:

- Prefer small commits.
- Group related page updates.
- Make changes reviewable.
- Do not mix schema changes with large content ingests unless necessary.

---

## 18. Agent Response Style

When reporting wiki work to the human, be concise and operational.

Use this format:

```markdown
## Result

Processed: ...
Created: ...
Updated: ...
Issues: ...
Next recommended action: ...
```

When blocked:

```markdown
## Blocked

Reason: ...
What I checked: ...
What is needed: ...
Safe next step: ...
```

Do not hide uncertainty.

---

## 19. Domain Adaptation Rules

This wiki may evolve for different domains. When adapting, prefer adding specialized subfolders only when the default topology becomes insufficient.

Examples:

For software/project knowledge:

```text
wiki/specs/
wiki/architecture/
wiki/experiments/
wiki/incidents/
```

For research:

```text
wiki/papers/
wiki/authors/
wiki/methods/
wiki/datasets/
```

For business/team knowledge:

```text
wiki/projects/
wiki/customers/
wiki/meetings/
wiki/risks/
```

Do not create many folders prematurely. Start simple, then specialize.

---

## 20. Definition of Done

A wiki operation is complete only when:

- The relevant source has been read or explicitly marked unavailable.
- New knowledge has been integrated into existing pages where appropriate.
- Duplicate pages were avoided.
- Links and backlinks were considered.
- Provenance was recorded.
- Contradictions were surfaced.
- `wiki/index.md` was updated.
- `wiki/log.md` was appended.
- Any uncertainty was clearly marked.
- `scripts/release_gate.sh` passed.
- The final human response states what changed.

---

## 21. Default Commands for the Agent

These are conceptual commands. Implement them as scripts, prompts, or manual workflows as needed.

### `wiki ingest <source>`

Process one source into the wiki.

Expected output:

- Source page.
- Updated related pages.
- Ingest report.
- Index update.
- Log entry.

### `wiki query <question>`

Answer using the wiki first, then raw sources if needed.

Expected output:

- Grounded answer.
- Relevant links.
- Optional query report.
- Optional promoted wiki page.

### `wiki lint`

Health-check the wiki.

Expected output:

- Lint report.
- Safe repairs.
- Human-review list.

### `wiki map <topic>`

Create a navigational map for a topic cluster.

Expected output:

- Map page.
- Reading path.
- Related concepts.
- Open questions.

### `wiki decide <decision>`

Record a durable decision.

Expected output:

- Decision page.
- Updated related pages.
- Log entry.

---

## 22. First-Time Initialization Checklist

When initializing a new wiki:

- [ ] Create the default vault topology.
- [ ] Create `wiki/index.md`.
- [ ] Create `wiki/log.md`.
- [ ] Create `wiki/inbox.md`.
- [ ] Create `wiki/overview.md`.
- [ ] Add this `AGENTS.md`.
- [ ] Ingest the first source.
- [ ] Create initial concept pages.
- [ ] Create first map page if useful.
- [ ] Commit the initial wiki state.

---

## 23. Minimal Initial Pages

Create these pages early:

```text
wiki/overview.md
wiki/concepts/compiled-knowledge.md
wiki/concepts/llm-maintained-wiki.md
wiki/concepts/source-of-truth.md
wiki/workflows/ingest-new-source.md
wiki/workflows/wiki-linting.md
wiki/decisions/use-markdown-for-compiled-knowledge.md
wiki/maps/wiki-operating-map.md
```

---

## 24. Maintenance Rhythm

Recommended cadence:

| Frequency | Action |
|---|---|
| Every source | Run ingest workflow. |
| Every useful query | Consider filing answer into wiki. |
| Weekly | Run lint workflow. |
| Monthly | Review overview, open questions, and stale pages. |
| After major project changes | Update systems, decisions, workflows, and maps. |

---

## 25. Topology Usability Test

When reviewing the vault topology, apply this test:

| Question | Pass Condition |
|---|---|
| Can the human start by using only `raw/`, `wiki/overview.md`, and `wiki/index.md`? | Yes. |
| Can the agent decide page placement without asking the human every time? | Yes. |
| Are optional folders avoided until there is enough content? | Yes. |
| Can a new agent understand the vault after reading `AGENTS.md`, `index.md`, `log.md`, and `overview.md`? | Yes. |
| Is every durable page reachable from `index.md` or a map? | Yes. |
| Can the topology grow without reorganizing everything? | Yes. |

If any condition fails, simplify the topology before adding more content.

---

## 26. Final Instruction to the Agent

Optimize for compounding value with minimal human bookkeeping.
