# Local Vault Authority Transition Design

**Date:** 2026-07-31  
**Status:** Approved direction; implementation planning pending  
**Target:** `/Users/junhyounglee/vault`  
**Builder:** `/Users/junhyounglee/workspace/vault-builder`

## 1. Purpose

Prepare the user's legacy Vault for the version 1 authority contract without
losing derived knowledge, duplicating semantic bodies, or weakening the
existing migration safety contract.

The transition must leave:

- `raw/` as immutable evidence;
- canonical wiki pages as the only owners of reusable explanations;
- `wiki/sources/` as registration and provenance manifests;
- builder-managed operating files aligned with the current builder contract;
- Vault migration execution inside the existing recoverable apply transaction.

## 2. Current State

The 2026-07-31 read-only audit reported:

- current contract: legacy;
- target contract: 1;
- `create`: 3;
- `replace`: 7;
- `review-required`: 29;
- `rebuild-suggested`: 9;
- `invalid`: 0.

The review set consists of five builder-managed operating files and 24 legacy
source pages with semantic bodies.

The target Vault is clean at commit `d86031e` before transition work begins.

## 3. Chosen Approach

Use a Git-backed preflight reconciliation followed by the existing migration
transaction.

Do not add a force flag, edit an audit plan, or add a general approval bypass
to `vault-builder`. Unknown managed hashes must continue to block normal
apply. The preflight reconciliation makes reviewed content explicit and
recoverable; a fresh audit then determines whether the target is eligible for
apply.

## 4. Work Isolation

Create an isolated Vault branch named `codex/vault-authority-migration-v1`
from clean `main`.

All knowledge transition changes are committed before migration apply. This
commit is the recovery boundary for human-judgment content. Existing user
commits remain untouched.

Do not stage or commit unrelated files if new unrelated work appears during
the transition.

## 5. Source Manifest Transition

Process each of the 24 legacy source pages independently.

### 5.1 Classification

For every source page:

1. verify that `canonical_source` exists and its `raw_sha256` matches the raw
   source;
2. inventory existing non-source canonical wiki links;
3. compare the legacy semantic sections with those linked canonical pages;
4. classify each derived statement as already owned or not yet owned.

Self-links under `wiki/sources/` are provenance links, not canonical ownership.

### 5.2 Selective Preservation

If existing canonical pages own the derived knowledge, do not create another
semantic body. Rewrite the source page as a reference-only manifest and link
those canonical pages.

If material derived knowledge is not owned elsewhere, move that knowledge into
the smallest suitable canonical page:

- concepts and principles under `wiki/concepts/`;
- systems and architectures under `wiki/systems/`;
- repeatable processes under `wiki/workflows/`;
- accepted choices under `wiki/decisions/`;
- comparisons under `wiki/comparisons/`.

Prefer updating an existing canonical page over creating a new page when the
existing page has the same subject and authority. Create a new page only when
no existing page can own the content without mixing unrelated subjects.

Moving means the semantic body has one durable owner after the transition. Do
not leave the same explanation in both the source manifest and a canonical
page.

If any derived statement has no clear canonical owner, the affected source
page stays unchanged and the entire preflight remains incomplete. Do not make
that source reference-only and do not proceed to migration apply until the
ownership decision is resolved.

### 5.3 Final Manifest Shape

Each source manifest must contain:

- one H1 equal to its frontmatter title;
- `## Summary`;
- `## Registration`;
- `## Durable Knowledge Links`;
- the canonical raw path, SHA-256, and source kind;
- only wiki-link bullets or `- None.` under durable links.

The manifest must contain no copied claim table, fenced code, detailed
takeaways, contradiction analysis, or follow-up body.

### 5.4 Provenance

Canonical pages created or updated by this transition must retain provenance
to the raw source through their existing frontmatter and source references.
The source manifest links back to those pages. Raw source bytes are never
rewritten.

## 6. Builder-Managed File Reconciliation

The five reviewed operating files are:

- `README.md`;
- `docs/usage.md`;
- `scripts/release_gate.sh`;
- `tests/test_wiki_quality_baseline.py`;
- `tests/test_wiki_tools.py`.

These paths contain operating contracts and generated-Vault tests rather than
user knowledge. Before alignment, record a per-file disposition that explains:

- what differs from the current builder version;
- whether the difference is generic legacy behavior, intentional local
  operating behavior, or durable Vault knowledge;
- where any intentional local behavior or knowledge will be preserved;
- whether exact builder alignment is safe.

Align a file only when its disposition proves that the current difference is
superseded generic builder behavior or has been preserved elsewhere. If a
local behavior or knowledge difference cannot be safely preserved, leave the
file unchanged and keep the migration blocked for user judgment.

Record this alignment in the same preflight reconciliation commit. Do not
modify raw evidence, canonical wiki bodies, logs, reports, or unrelated
project-specific tooling as part of managed-file alignment.

## 7. Verification Before Apply

Before creating a new audit plan:

1. prove the complete `raw/` tree digest is unchanged from the clean baseline;
2. run authority validation, lint, health, and the Vault release gate;
3. confirm all 24 source manifests satisfy the version 1 source contract;
4. confirm every removed semantic section is either owned by an existing
   canonical page or present in an intentional new or updated canonical page;
5. append a concise `wiki/log.md` entry describing the authority transition;
6. commit only the preflight reconciliation;
7. run a fresh read-only audit with its plan outside the Vault;
8. present the exact fresh audit summary and managed-path dispositions to the
   user and obtain explicit approval for that plan before apply.

Apply is allowed only when:

- `review-required` is zero;
- `invalid` is zero;
- no recovery or preparation artifact exists;
- the audit target fingerprint matches the committed Vault state;
- the plan was generated by the current clean builder revision;
- the user explicitly approved the exact fresh migration ID after reviewing
  its summary.

## 8. Migration Apply

Run the existing `vault_migration apply` command using Python 3.10 or newer and
the fresh external plan.

The apply transaction may create or replace only catalog-managed operating
files and the version 1 manifest. It must not rewrite raw sources, canonical
wiki pages, source manifests, logs, reports, index files, or maps.

If apply reports rollback or cleanup recovery instructions, stop and execute
only the exact printed recovery command before any new audit or apply.

## 9. Post-Migration Generated Views

After apply and release-gate verification succeed:

1. commit the successful migration contract and manifest changes separately
   from the preflight authority transition;
2. rebuild `wiki/index.md` and navigation maps explicitly;
3. validate the rebuilt views;
4. commit generated-view changes separately from both the authority
   transition and migration contract change.

Generated views remain rebuildable projections and never become canonical
knowledge owners.

## 10. Final Verification

The work is complete only when:

- `.vault-builder/manifest.json` records contract version 1 and the successful
  migration ID;
- no recovery, preparation, cleanup, or directory-marker artifact remains;
- the complete release gate passes under the migrated contract;
- `raw/` has the same digest as the clean baseline;
- source manifests pass authority validation;
- canonical knowledge has no duplicate body introduced by the transition;
- all pre-existing unrelated Vault content remains present;
- a final audit reports no `review-required` or `invalid` actions;
- the transition branch is fast-forward merged into local `main` only after
  all checks pass.

## 11. Failure Handling

- A source statement whose canonical owner is unclear keeps its source page
  unchanged and blocks preflight completion and migration apply; do not delete
  it or guess an owner.
- A raw hash mismatch stops work on that source until the drift is understood.
- A validation failure is fixed on the transition branch before migration.
- A target change after audit invalidates the plan and requires a fresh audit.
- A migration failure follows the existing journal-based rollback or recovery
  protocol.
- Any unrelated concurrent Vault change stops reconciliation until its scope
  is separated from this transition.

## 12. Non-Goals

- No Vault Graph code-index or MCP implementation in this transition.
- No modification of raw evidence.
- No automatic semantic merging.
- No force, partial-apply, or edited-plan migration path.
- No deletion of legacy builder copies unless the version 1 contract
  explicitly manages them.
- No generated-view rebuild hidden inside migration apply.
