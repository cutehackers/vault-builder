# Patch Log

## 2026-07-31 - Local Vault Transition Approval Correction
**Trigger:** Independent review of the local Vault authority-transition design found ambiguous overwrite, approval, and unresolved-knowledge gates.
**Scope:** Managed-file reconciliation, source-body ownership transition, post-audit approval, and migration commit boundaries.
**Core Values Protected:** User customization preservation, explicit migration authority, single-body ownership, and recoverable history.
**Changes Applied:** Require per-file managed dispositions, block the full transition on unclear canonical ownership, require user approval of the exact fresh migration ID, update the operation log, and separate authority, migration, and generated-view commits.
**Verification:** Re-run independent spec review and require approval before implementation planning.

## 2026-07-30 - Final Publication And Authority Contract Correction
**Trigger:** Post-fix whole-review found final-boundary publication races, self-invalidating audit output, non-portable paired-output claims, source-role precedence gaps, and over-broad manifest path acceptance.
**Scope:** Managed, manifest, and rollback publication and recovery; audit artifact ownership and plan identity; role classification; shared manifest validation.
**Core Values Protected:** External-writer preservation, honest atomicity boundaries, migration-relevant plan stability, path-owned authority, and exact builder scope.
**Changes Applied:** Open every parent component through no-follow descriptors, validate atomic-exchange displacement, and reconcile durable staged content before recovery cleanup; quarantine rollback deletions with native no-replace moves; publish only one create-only audit artifact per invocation with a no-overwrite hard link, never clean unowned temp-shaped siblings, exclude reports but include supported manifest bytes in plan fingerprints; make `wiki/sources/` location authoritative while reporting type mismatches; accept only exact v1 catalog paths in shared manifests.
**Verification:** Descriptor-open and final-boundary races, interrupted managed/manifest/rollback exchange recovery, originally-absent rollback replacement, manifest-plan staleness, self-report, existing-output refusal, pre/post-publication hard stops, unowned-temp preservation, source precedence and exact manifest corruption matrices, plus full builder, wiki, generated-Vault, catalog, acceptance, and release gates.

## 2026-07-30 - Final Race Correction
**Trigger:** Ultimate whole-branch review after final contract correction.
**Scope:** Managed and manifest publication preconditions plus atomic audit plan/report output publication.
**Core Values Protected:** External-writer preservation, exact migration authority, transaction consistency, and independent disjoint output progress.
**Changes Applied:** Bind staged publication to an immediately revalidated absent or exact no-follow file state, including parsed manifest identity and last migration ID; lock unique audit output parent directories in deterministic device/inode order across preparation, commit, rollback, and cleanup; revalidate every output leaf immediately before replacement.
**Verification:** Focused final-publication and real-process output races; complete builder, wiki, catalog, acceptance, generated-Vault, release, grammar, compilation, shell, JSON, diff, and status gates.

## 2026-07-30 - Final Migration Contract Correction
**Trigger:** Final whole-branch contract review after durability correction.
**Scope:** Global migration serialization, stale-recovery fencing, resumable directory rollback, managed-file mode identity, and generated-Vault manifest validation.
**Core Values Protected:** Single-writer migration safety, exact rollback, mode-preserving ownership, and one shared contract schema.
**Changes Applied:** Hold one target-root process lock across apply/recover; block apply on every outstanding migration artifact and report all recovery commands; bind nofollow/fstat managed and manifest snapshots plus immediate publication checks to audited hash/mode/last migration ID; fence recovery to recorded prior/candidate manifest states; journal directory-removal progress across hard stops; classify and repair mode-only drift while restoring prior modes; reuse a complete v1 manifest validator that rejects control-bearing paths in builder and generated Vault tooling.
**Verification:** Concurrent apply and process-crash lock regressions; managed and manifest pre-snapshot, mid-snapshot, intent, and pre-publication races; two-migration and stale-journal regressions; marker/rmdir hard stops; mode audit/apply/rollback/manifest tests; direct, lint, validate-page, generated-gate corruption matrices; full builder, wiki, catalog, acceptance, fresh, and migrated gates.

## 2026-07-30 - Migration Durability And Cleanup Lifecycle Correction
**Trigger:** Final whole-branch durability re-review after the integration correction.
**Scope:** Directory-entry persistence, journal-contained publication staging, preparation/rollback authority, and committed cleanup recovery.
**Core Values Protected:** Crash-safe WAL ordering, exact rollback, committed-state preservation, and actionable idempotent recovery.
**Changes Applied:** Propagate fatal directory fsync failures; establish target-root preparation authority before recovery ancestry; durably stage managed, prior/candidate manifest, and rollback publications; move committed and rolled-back journals through phase-specific tombstones; make `recover` resume preparation, rollback, or committed cleanup without crossing state boundaries.
**Verification:** Focused fsync injection; nested preparation, managed/manifest, rollback, marker, tombstone-rename, partial-deletion, and final-fsync hard stops; full builder, generated-Vault, catalog, wiki, and acceptance gates.

## 2026-07-30 - Final Branch Integration Correction
**Trigger:** Final whole-branch review after Task 8 acceptance.
**Scope:** Candidate-manifest write-ahead recovery, authority strictness, transactional apply errors, and generated-Vault topology documentation.
**Core Values Protected:** Crash-safe migration, exact authority contracts, actionable recovery, and builder-only creation ownership.
**Changes Applied:** Journal prior-manifest backup and candidate publication lifecycles before each write; preflight all targets, originals, and candidate-required prior backups before rollback mutation; restore manifests first; reject unsupported contracts and Registration extras; type preparation/apply rollback outcomes; remove builder-only init tooling from the generated tree.
**Verification:** Focused backup/publication hard stops, automatic and explicit zero-mutation recovery, authority lint/CLI, and failure-contract regressions plus full builder, generated-Vault, catalog, and acceptance gates.

## 2026-07-30 - Dual-Environment Gate Correction
**Trigger:** Task 7 dual-environment gate verification.
**Scope:** Deterministic legacy fixture modes and builder-only test skip boundaries.
**Core Values Protected:** Exact migration validation and generated-Vault independence.
**Changes Applied:** Normalize legacy fixture modes from v0 catalog; run migration tests in the builder checkout and skip only absent creation-tool tests.
**Verification:** Builder and generated-Vault release gates pass.

## 2026-07-30 - Migrated Vault Builder Identity Correction
**Trigger:** Task 8 actual legacy two-gate acceptance.
**Scope:** Builder-only creation-test identity predicate.
**Core Values Protected:** Legacy copy preservation, generated-Vault independence, and two-gate rollback safety.
**Changes Applied:** Require the current migration package and builder migration suite, in addition to init/bootstrap scripts, before running builder-only creation tests.
**Verification:** Current builder, fresh generated Vault, and migrated legacy Vault gates plus end-to-end acceptance pass.
