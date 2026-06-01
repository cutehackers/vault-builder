---
title: "Wiki Log"
type: log
status: active
created: 2026-05-26
updated: 2026-05-26
owner: agent
summary: "Append-only operation history for the LLM Wiki."
source_count: 0
tags: [log, operations]
related:
  - index
  - workflows/wiki-linting
confidence: high
quality:
  provenance: none
  links: unchecked
  contradictions: none
  review_required: false
---

# Wiki Log

## Event Types

- ingest
- query
- lint
- repair
- decision
- schema-change
- manual-note

## [2026-05-26] schema-change | Add Quality-Gated Wiki Schema

- Updated operating schema to distinguish LLM semantic drafts from durable compiled wiki pages.
- Added quality frontmatter fields for provenance, link health, contradiction state, and review requirements.
- Added ephemeral staging areas: `scratch/drafts/` and `scratch/review/`.
- Contradictions found: none.
- Follow-ups: implement deterministic validation under `tools/wiki/`.

## [2026-05-26] repair | Normalize Initial Wiki Baseline

- Added frontmatter to core pages.
- Added initial concept, workflow, decision, and map pages.
- Updated index navigation.
- Contradictions found: none.
- Follow-ups: add a deterministic vault linter.

## [2026-05-26] decision | Fix Next Step As Stenc Spec And Plan

- Added Stenc spec: `docs/stenc/content/specs/2026-05-26-wiki-quality-validator.spec.json`
- Added Stenc plan: `docs/stenc/content/plans/2026-05-26-wiki-quality-validator-implementation.plan.json`
- Regenerated Stenc static docs under `docs/stenc/`.
- Contradictions found: none.
- Follow-ups: implement `tools/wiki` validator and lint commands according to the plan.

## [2026-05-26] repair | Implement Wiki Quality Validator

- Added `tools/wiki/cli.py` with `validate-page` and `lint` commands.
- Added deterministic checks for frontmatter schema, links, index coverage, log format, provenance signals, and human-lock markers.
- Added `tests/test_wiki_tools.py` for validator behavior.
- Added lint report: `scratch/reports/2026-05-26-lint.md`
- Contradictions found: none.
- Follow-ups: plan `apply-draft` after validator behavior settles.

## [2026-05-26] manual-note | Define Final LLM Wiki Architecture

- Added architecture definition: `docs/LLM-WIKI.md`
- Clarified the quality-gated compiled wiki model, layer responsibilities, frontmatter boundary, workflows, and definition of done.
- Contradictions found: none.
- Follow-ups: keep `docs/LLM-WIKI.md`, `AGENTS.md`, and Stenc implementation docs aligned as tooling evolves.

## [2026-05-26] repair | Harden Wiki Quality Validator After Review

- Added checks for orphan durable pages, malformed log headings, malformed frontmatter, required field types, human-lock ordering, and grouped lint reports.
- Updated `docs/LLM-WIKI.md`, `wiki/index.md`, `wiki/overview.md`, and README for architecture discoverability and source-of-truth clarity.
- Marked the Stenc implementation plan as canonical after implementation.
- Contradictions found: source-of-truth wording was ambiguous between operating priority and factual evidence priority; resolved by splitting the hierarchy in `docs/LLM-WIKI.md`.
- Follow-ups: plan `apply-draft` or source hashing as a separate Stenc document.

## [2026-05-28] schema-change | Split Agent Contract And Add Source Gates

- Reduced `AGENTS.md` to a concise operational entrypoint.
- Moved the detailed schema reference to `docs/agent/OPERATING-SCHEMA.md`.
- Added `hash-source`, `ingest-source`, and `apply-draft` command surfaces.
- Added source drift checking through `canonical_source` and `raw_sha256`.
- Added `tools/wiki/templates/draft-upsert-page.json` and `docs/agent/DRAFTS.md` for semantic draft handoff.
- Added `tools/wiki/mcp_server.py` for stdio MCP access to stable wiki tools.
- Contradictions found: none.
- Follow-ups: add richer semantic extraction prompts and merge/review helpers.

## [2026-05-30] repair | Rebuild Navigation Maps

- Updated [[maps/topic-map]].
- Updated [[maps/source-map]].
- Updated [[maps/decision-map]].
- Updated [[maps/review-map]].
- Updated [[maps/lifecycle-map]].

## [2026-05-30] repair | Upgrade Baseline Claim Provenance

- Upgraded core seed pages to claim-level provenance tables.
- Used explicit human instruction in `AGENTS.md` as the shared evidence reference.

## [2026-05-31] repair | Align Draft Publication Boundary

- Updated [[workflows/ingest-new-source]] to publish semantic drafts through `publish-draft`.
- Updated [[overview]] to describe transactional JSON draft publication.

## [2026-05-31] repair | Rebuild Navigation Maps

- Updated [[maps/topic-map]].

## [2026-05-31] repair | Align Review Hardening Documentation

- Updated [[workflows/wiki-linting]] to remove auto-fix overstatement and include review reference validation.
- Updated [[overview]], [[index]], and map entry points to reflect the current tool surface.

## [2026-05-31] repair | Harden Remaining Wiki Operations

- Updated [[overview]] with configurable metrics and repo-local release gate status.
- Updated [[workflows/wiki-linting]] with wiki-root link boundary checks.

## [2026-05-31] repair | Close Subagent Hardening Findings

- Updated [[overview]] with scratch-confined artifacts and CI release gate status.
- Recorded final hardening after subagent review of MCP, metrics policy, release packaging, and path boundaries.

## [2026-06-01] schema-change | Make Stenc Optional In Builder

- Changed default vault-builder bootstrap and `init-vault.sh` paths so Stenc docs and tools are excluded unless `--with-stenc` or `VAULT_BOOTSTRAP_WITH_STENC=1` is requested.
- Changed no-argument bootstrap default target from `llm-wiki` to `vault` and aligned quick-start examples with the generated folder name.
- Changed release gate behavior so Stenc validation/render checks run only when `WIKI_ENABLE_STENC=1`.
- Rewrote `tools/wiki/README.md` as a concise skill-first user guide and added `tools/wiki/README-kr.md`.
- Replaced root `README.md` with the concise skill-first guide and added root `README-kr.md`.
- Restored the one-line GitHub raw bootstrap install command in root README files, targeting `cutehackers/vault-builder`.
- Added copy-ready example prompts for `wiki-ingest`, `wiki-update`, and `wiki-query` to root README files.
- Removed now-redundant `tools/wiki/README.md` and `tools/wiki/README-kr.md`; root README files are the entry documentation.
- Updated docs and tests to preserve the core vault-builder install workflow without requiring a user-installed Stenc skill.
- Hardened generated no-Stenc vault tests so Stenc opt-in checks skip when the optional bundle is absent.
- Changed `--with-stenc` initialization to fail clearly when the template does not include Stenc assets.
- Sanitized builder full-bootstrap release-gate execution so bootstrap-only env does not trigger recursive full verification.
- Added an explicit `tests` package marker so generated vault release gates do not import a shadowing site-package.
- Aligned direct `init-vault.sh` execution with bootstrap so no-argument installs create `vault/` by default.
- Contradictions found: prior builder output treated Stenc as part of the default install path, while current human instruction defines Stenc as optional.
- Follow-ups: use `--with-stenc` only for vaults that intentionally keep fixed-format Stenc spec/plan docs.

## [2026-06-01] schema-change | Align Repo-Local Skill Path In Builder

- Moved repo-local wiki skill documentation from `agents/skills/` to `.agents/skills/`.
- Updated `init-vault.sh` so generated vaults install wiki skills under `.agents/skills/`.
- Updated agent, README, architecture, usage, and Stenc planning references to the new Codex-compatible path.
- Contradictions found: prior builder output used `agents/skills/`, while the current agent convention expects `.agents/skills/`.
- Follow-ups: keep generated vault tests asserting the old `agents/skills/` path is absent.
