# Local Vault Authority Transition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile the user's clean legacy Vault with the version 1 authority contract, preserve every derived knowledge body under one canonical owner, and produce a fresh non-blocking migration plan for explicit user approval.

**Architecture:** Perform human-judgment knowledge reconciliation on an isolated Vault branch and preserve it in Git before structural migration. Rewrite legacy source pages as reference-only manifests only after recording their canonical owners, align five reviewed operating files with the current builder, then use the existing read-only audit and recoverable apply transaction without adding force or partial-apply behavior.

**Tech Stack:** Git worktrees and branches, Markdown/YAML frontmatter, Python 3.12 standard library tools, `unittest`, existing `tools/wiki` validators, existing `tools/vault_migration` audit/apply/recover commands, Bash release gates.

---

## File Structure

### Builder Repository

- Reference: `docs/superpowers/specs/2026-07-31-local-vault-authority-transition-design.md`
- Track execution: `docs/superpowers/plans/2026-07-31-local-vault-authority-transition.md`
- Read migration implementation: `tools/vault_migration/`
- Read desired managed bytes: the five builder-managed paths listed below.

No builder production code changes are planned.

### Target Vault

- Create: `scratch/reports/authority-transition/2026-07-31-managed-file-dispositions.md`
  - Records file-by-file review evidence and source-to-canonical ownership mapping.
- Modify: 24 files under `wiki/sources/`
  - Replace legacy semantic bodies with reference-only manifest bodies.
- Modify only if the ownership review finds an uncovered statement:
  - `wiki/concepts/*.md`
  - `wiki/systems/*.md`
  - `wiki/workflows/*.md`
  - `wiki/decisions/*.md`
  - `wiki/comparisons/*.md`
  - `wiki/timelines/rail-harness-evolution-202604-202605.md`
- Align with builder:
  - `README.md`
  - `docs/usage.md`
  - `scripts/release_gate.sh`
  - `tests/test_wiki_quality_baseline.py`
  - `tests/test_wiki_tools.py`
- Modify: `wiki/log.md`
- Created by migration apply after a separate approval:
  - `.vault-builder/manifest.json`
- Rebuild after successful apply:
  - `wiki/index.md`
  - `wiki/maps/*.md`

## Safety Rules Used by Every Task

- Use `/Users/junhyounglee/vault` only as the final integration checkout.
- Work in an isolated target worktree on branch
  `codex/vault-authority-migration-v1`.
- Never modify any file below `raw/`.
- Do not stage unrelated changes.
- Use Python 3.12, not `/usr/bin/python3` 3.9, for migration commands.
- Never edit an audit plan.
- Stop before apply and request approval for the exact fresh migration ID.
- If any canonical owner is unclear, leave the source page unchanged and stop
  the full transition.
- If apply prints recovery instructions, run only the exact printed command
  before any new audit or apply.

### Task 1: Create the Target Worktree and Immutable Baseline

**Files:**
- Read: `/Users/junhyounglee/vault/AGENTS.md`
- Create outside Git: worktree for `codex/vault-authority-migration-v1`
- Create outside target: baseline status and raw digest evidence

- [ ] **Step 1: Verify the integration checkout is clean and current**

Run:

```bash
git -C /Users/junhyounglee/vault status --short --branch
git -C /Users/junhyounglee/vault rev-parse HEAD
git -C /Users/junhyounglee/vault rev-parse origin/main
```

Expected: clean `main`, and both revisions equal `d86031e` or the same newer
user-approved clean revision.

- [ ] **Step 2: Read the target operating contract and wiki update workflow**

Read completely:

```text
/Users/junhyounglee/vault/AGENTS.md
/Users/junhyounglee/vault/.agents/skills/wiki-update/SKILL.md
```

Expected: raw immutability, provenance, human-lock, index/log, and validation
rules are understood before edits.

- [ ] **Step 3: Create an isolated Vault worktree**

Use the existing ignored `.worktrees/` directory if present; otherwise use the
global Superpowers worktree directory after following the worktree skill.

```bash
git -C /Users/junhyounglee/vault worktree add \
  /Users/junhyounglee/vault/.worktrees/vault-authority-migration-v1 \
  -b codex/vault-authority-migration-v1
```

Expected: new worktree at the exact path and the integration checkout remains
on clean `main`.

- [ ] **Step 4: Capture baseline Git state and raw digest**

From the target worktree, compute a deterministic no-follow digest over raw
file path, type, mode, and SHA-256. Store the output outside the target Vault
under the builder worktree's ignored `.superpowers/` evidence directory.

Expected:

- baseline commit recorded;
- raw entry count recorded;
- raw tree digest recorded;
- target Git status recorded as clean.

- [ ] **Step 5: Run the target baseline release gate**

Run:

```bash
scripts/release_gate.sh
```

Expected: exit 0. If it fails, stop and diagnose the pre-existing failure
before editing.

### Task 2: Record Managed-File and Source-Ownership Dispositions

**Files:**
- Create: `scratch/reports/authority-transition/2026-07-31-managed-file-dispositions.md`
- Read: five managed files in target and builder
- Read: all 24 legacy source manifests and their candidate canonical owners

- [ ] **Step 1: Create the report skeleton**

The report must contain:

```markdown
# Vault Authority Transition Review — 2026-07-31

## Baseline

## Managed File Dispositions

| Path | Difference Classification | Preserved At | Alignment Safe | Evidence |
|---|---|---|---|---|

## Source Ownership Dispositions

| Source Manifest | Raw Source | Canonical Owners | Uncovered Knowledge | Decision |
|---|---|---|---|---|

## Verification
```

- [ ] **Step 2: Review `README.md`**

Compare target and builder versions. Record whether removed long-form operating
instructions are superseded by current builder docs and whether any
Vault-specific knowledge exists.

Expected disposition: align only if every difference is generic builder
documentation or preserved in current `docs/usage.md` and operating schema.

- [ ] **Step 3: Review `docs/usage.md`**

Compare target and builder versions. Record bootstrap, migration, recovery,
and user-workflow differences.

Expected disposition: align only if the builder version is a strict
operational successor and no local Vault knowledge is removed.

- [ ] **Step 4: Review `scripts/release_gate.sh`**

Compare target and builder versions. Record Python selection, raw whitespace
coverage, builder-test routing, and target-Vault behavior.

Expected disposition: align only if current builder behavior preserves target
Vault validation and adds no builder-only dependency to generated Vaults.

- [ ] **Step 5: Review both test files**

Compare:

```text
tests/test_wiki_quality_baseline.py
tests/test_wiki_tools.py
```

Record whether changes only encode the newer shared authority and generated
Vault contracts.

Expected disposition: align only if no target-specific test or local workflow
behavior is lost.

- [ ] **Step 6: Inventory all 24 source manifests**

For every path in the fresh audit's `review-required` source set, record:

- canonical raw path and current raw digest;
- existing non-source wiki links;
- relevant reverse links from canonical pages;
- semantic sections present in the legacy body;
- one or more canonical owners for every durable statement;
- whether any statement is uncovered.

Use these canonical page families first:

- Codex and harness:
  - `wiki/systems/codex-development-harness.md`
  - `wiki/systems/modern-agent-harness.md`
  - `wiki/systems/skill-first-sdk-free-codex-harness.md`
  - `wiki/comparisons/codex-harness-integration-models.md`
  - `wiki/concepts/task-contract.md`
  - `wiki/workflows/codex-verification-tiering.md`
  - `wiki/workflows/codex-harness-retrospective-loop.md`
- Compiled wiki and OKF:
  - `wiki/concepts/compiled-knowledge.md`
  - `wiki/concepts/llm-maintained-wiki.md`
  - `wiki/concepts/open-knowledge-format.md`
  - `wiki/concepts/retrieval-augmented-compiled-knowledge.md`
  - `wiki/comparisons/open-knowledge-format-vs-rag.md`
  - `wiki/systems/hybrid-compiled-wiki-retrieval-layer.md`
  - `wiki/workflows/research-to-durable-capture.md`
  - `wiki/workflows/okf-bundle-authoring.md`
- Rail:
  - `wiki/timelines/rail-harness-evolution-202604-202605.md`
  - `wiki/comparisons/rail-authority-models.md`
  - `wiki/concepts/rail-artifact-authority.md`
  - `wiki/concepts/rail-broker-operations.md`
  - `wiki/decisions/rail-modernization-direction.md`
  - `wiki/systems/rail-core-v2-production-readiness.md`
  - `wiki/decisions/adopt-rail-core-v2-production-readiness-direction.md`
- Android and Wonderous:
  - `wiki/systems/race981-wonderous-ux-adaptation.md`
  - `wiki/concepts/data-driven-layered-parallax-illustration.md`
  - the five related Flutter workflow pages.
- Vault Graph:
  - `wiki/systems/vault-graph.md`
  - `wiki/concepts/agent-context-pack.md`
  - `wiki/workflows/read-only-rebuildable-indexing.md`
  - `wiki/decisions/build-vault-graph-as-read-only-projection-layer.md`

- [ ] **Step 7: Stop on any uncovered knowledge**

Expected: every durable statement has a named canonical owner. If not, update
or create the smallest appropriate canonical page through a validated wiki
draft before continuing. Every created or updated canonical page must cite the
exact raw source through its frontmatter/source references and preserve the
existing evidence chain. Record the exact destination and provenance path in
the report.

- [ ] **Step 8: Validate report completeness**

Expected:

- five managed-file rows;
- 24 source-manifest rows;
- no blank owner or decision field;
- every `Alignment Safe` value is evidence-backed;
- no claim that semantic equivalence was determined automatically.

### Task 3: Rewrite the 24 Source Pages as Reference-Only Manifests

**Files:**
- Modify: the exact 24 `wiki/sources/*.md` paths listed by the audit
- Modify only if Task 2 found a gap: exact canonical page named in the report

- [ ] **Step 1: Preserve frontmatter identity and provenance**

For each source page, keep its title, type, status, dates where appropriate,
owner, summary, `canonical_source`, `raw_sha256`, `source_kind`,
`source_count`, tags, related links, confidence, and quality metadata unless a
specific correction is required by validation.

- [ ] **Step 2: Render the required body**

Use exactly this shape:

```markdown
# <frontmatter title>

## Summary

<exact one-line frontmatter summary>

Source: `<canonical_source>`

## Registration

- Canonical source: `<canonical_source>`
- SHA-256: `<raw_sha256>`
- Source kind: `<source_kind>`

## Durable Knowledge Links

- [[<canonical owner 1>]]
- [[<canonical owner 2>]]
```

Use `- None.` only when the source genuinely produced no reusable durable
knowledge. Do not use source self-links as canonical owners.

- [ ] **Step 3: Rewrite the seven directly linked source families**

Update:

```text
wiki/sources/codex-best-in-class-harness-guide-20260611.md
wiki/sources/llm-wiki-system-models-research-20260604.md
wiki/sources/moder-agent-harnes-report-20260601.md
wiki/sources/okf-research-report-20260629.md
wiki/sources/rail-core-v2-production-readiness-prd-20260602-spec.md
wiki/sources/skillgraph-harness-design-ko20260601.md
wiki/sources/vault-graph-spec-20260604.md
```

Expected: durable links point only to canonical pages confirmed by Task 2.

- [ ] **Step 4: Rewrite the Android and Wonderous source families**

Update:

```text
wiki/sources/proandroiddev-android-google-io-2026-20260520.md
wiki/sources/sgp-981-app-research-wonderous-ux-patterns-20260622.md
```

Expected: links name the exact Android/Flutter/Wonderous canonical owners from
Task 2; self-links are removed.

- [ ] **Step 5: Rewrite the 15 Rail history and product source families**

Update:

```text
wiki/sources/rail-architecture-20260601.md
wiki/sources/rail-historical-plan-2026-04-15-rail-go-product-architecture.md
wiki/sources/rail-historical-plan-2026-04-27-actor-backend-port-and-result-projection.md
wiki/sources/rail-historical-plan-2026-04-29-python-actor-runtime-rail-redesign.md
wiki/sources/rail-historical-plan-2026-05-04-control-plane-brokered-actor-architecture.md
wiki/sources/rail-historical-spec-2026-04-15-rail-go-product-architecture-design.md
wiki/sources/rail-historical-spec-2026-04-21-codex-boundary-actor-backend-design.md
wiki/sources/rail-historical-spec-2026-04-27-actor-backend-port-and-result-projection-design.md
wiki/sources/rail-historical-spec-2026-04-29-python-actor-runtime-rail-redesign.md
wiki/sources/rail-historical-spec-2026-05-03-artifact-goal-context-architecture-design.md
wiki/sources/rail-historical-spec-2026-05-04-control-plane-brokered-actor-architecture-design.md
wiki/sources/rail-historical-spec-2026-05-18-rail-runner-schema-actor-runtime-design.md
wiki/sources/rail-historical-superpowers-readme-20260601.md
wiki/sources/rail-product-spec-20260601.md
wiki/sources/rail-readme-20260601.md
```

Expected: links use the Rail timeline, authority comparison, concepts,
decisions, and systems that actually own each source's derived knowledge.

- [ ] **Step 6: Run external version 1 authority validation**

From the builder worktree, run the current authority scanner against the target
worktree without creating a manifest.

Expected:

- zero source-manifest authority issues;
- zero duplicate canonical body issues introduced by this task;
- no source page retains legacy semantic headings, claim tables, or fenced
  code.

- [ ] **Step 7: Verify raw immutability**

Recompute the raw digest.

Expected: entry count and digest exactly equal Task 1 baseline.

### Task 4: Align Reviewed Builder-Managed Files

**Files:**
- Modify: the five reviewed managed files
- Read: Task 2 disposition report

- [ ] **Step 1: Confirm all five dispositions are safe**

Expected: every row has `Alignment Safe: yes`. If any row is not safe, stop
and request user judgment rather than aligning a partial set.

- [ ] **Step 2: Copy exact desired bytes and modes from the builder worktree**

Use a mechanical exact copy for only these five paths:

```text
README.md
docs/usage.md
scripts/release_gate.sh
tests/test_wiki_quality_baseline.py
tests/test_wiki_tools.py
```

Expected: target SHA-256 and mode for each path equal the version 1 catalog.

- [ ] **Step 3: Prove no other target path changed**

Run `git diff --name-only` and compare it with:

- the 24 source manifests;
- any intentional canonical pages named in the report;
- the five managed files;
- the transition report;
- `wiki/log.md` after Task 5.

Expected: no unrelated path appears.

### Task 5: Validate and Commit the Preflight Reconciliation

**Files:**
- Modify: `wiki/log.md`
- Commit: source manifests, any canonical updates, five managed files, report,
  and log

- [ ] **Step 1: Append the operation log entry**

Record:

- date;
- transition purpose;
- 24 source manifests changed to reference-only ownership;
- canonical pages created or updated, if any;
- five managed files aligned after disposition review;
- raw digest preserved;
- migration apply still pending a fresh audit and user approval.

- [ ] **Step 2: Run page-level and authority checks**

Run current builder validation against all 24 source manifests and any changed
canonical page.

Expected: zero errors.

- [ ] **Step 3: Run target lint and health**

Run:

```bash
python3.12 tools/wiki/cli.py lint --report
python3.12 tools/wiki/cli.py health
```

Expected: exit 0 and no authority issue.

- [ ] **Step 4: Run the full target release gate**

Run:

```bash
scripts/release_gate.sh
```

Expected: wiki suite passes; builder-only migration suite is not required in
the generated Vault; lint, health, maps check, metrics, and diff guard pass.

- [ ] **Step 5: Verify raw and unrelated content**

Expected:

- raw digest equals Task 1 baseline;
- no raw path is in `git diff`;
- no unrelated path is in `git diff`;
- no recovery or `.vault-builder` state exists yet.

- [ ] **Step 6: Commit the preflight reconciliation**

Stage only the reviewed target paths and commit:

```bash
git commit -m "docs(vault): reconcile authority ownership for migration"
```

Expected: target worktree is clean after the commit.

### Task 6: Produce the Fresh Migration Plan and Approval Brief

**Files:**
- Create outside target:
  - `plan.json`
  - audit summary evidence
- Read: committed target state and Task 2 disposition report

- [ ] **Step 1: Run a fresh read-only audit**

Use Python 3.12 from the clean builder worktree:

```bash
python3.12 tools/vault_migration/cli.py audit \
  --target /Users/junhyounglee/vault/.worktrees/vault-authority-migration-v1 \
  --plan-out <external-create-only-plan-path>
```

Expected:

- `review-required: 0`;
- `invalid: 0`;
- target writes: none;
- plan exists outside the Vault.

- [ ] **Step 2: Prove audit read-only behavior**

Expected:

- target commit unchanged;
- target Git status unchanged and clean;
- raw digest unchanged;
- no `.vault-builder` state or report created in target.

- [ ] **Step 3: Verify apply safety prerequisites**

Verify:

- plan builder revision equals current v1 catalog;
- target fingerprint matches a fresh computation;
- no recovery, preparation, cleanup, or marker artifact exists;
- builder worktree is clean;
- target worktree is clean.

- [ ] **Step 4: Present the exact approval brief and stop**

Present:

- exact migration ID;
- target commit;
- action counts and exact create/replace paths;
- managed-file dispositions;
- source ownership completion result;
- raw digest equality;
- release-gate result;
- recovery status.

Do not run apply until the user explicitly approves this exact migration ID.

### Task 7: Apply the Approved Migration

**Precondition:** User explicitly approved the exact Task 6 migration ID.

**Files:**
- Create/replace only catalog-managed paths listed by the approved plan
- Create: `.vault-builder/manifest.json`

- [ ] **Step 1: Reconfirm plan and target identity**

Expected: target commit, fingerprint, builder revision, and migration ID still
match Task 6.

- [ ] **Step 2: Run apply**

```bash
python3.12 tools/vault_migration/cli.py apply \
  --target /Users/junhyounglee/vault/.worktrees/vault-authority-migration-v1 \
  --plan <approved-external-plan-path>
```

Expected: exit 0, both release gates pass, manifest records contract 1 and the
approved migration ID, and no recovery instruction is printed.

- [ ] **Step 3: Handle recovery if and only if instructed**

If apply prints an exact `recover` command, stop and run only that command.
Do not re-audit or re-apply first.

- [ ] **Step 4: Verify migrated contract**

Run the complete target release gate and a final read-only audit.

Expected:

- release gate exit 0;
- final audit has `review-required: 0` and `invalid: 0`;
- no recovery artifacts;
- raw digest unchanged.

- [ ] **Step 5: Commit migration metadata and managed changes**

Commit separately:

```bash
git commit -m "chore(vault): record authority contract v1 migration"
```

### Task 8: Rebuild Generated Views and Integrate

**Files:**
- Modify: `wiki/index.md`
- Modify: `wiki/maps/*.md`

- [ ] **Step 1: Rebuild generated views explicitly**

Run:

```bash
python3.12 tools/wiki/cli.py maps build --report
```

Expected: this deterministic command rebuilds navigation maps and refreshes
`wiki/index.md` and `wiki/log.md`.

- [ ] **Step 2: Validate generated views and full Vault**

Run:

```bash
python3.12 tools/wiki/cli.py maps build --check --report
scripts/release_gate.sh
```

Expected: exit 0 and no generated drift.

- [ ] **Step 3: Commit generated views separately**

```bash
git commit -m "docs(vault): rebuild authority navigation views"
```

- [ ] **Step 4: Final evidence review**

Run a new read-only audit from the clean builder worktree after the
generated-view commit:

```bash
python3.12 tools/vault_migration/cli.py audit \
  --target /Users/junhyounglee/vault/.worktrees/vault-authority-migration-v1
```

Verify:

- three intentional commit boundaries: authority transition, migration,
  generated views;
- raw digest unchanged;
- clean target worktree;
- final post-generated-view audit has `review-required: 0` and `invalid: 0`;
- no recovery artifacts.

- [ ] **Step 5: Fast-forward local main**

After final verification, fast-forward the integration checkout's local
`main` to `codex/vault-authority-migration-v1`, rerun the release gate on
`main`, remove the worktree, and delete the feature branch.

Do not push unless the user explicitly requests it.
