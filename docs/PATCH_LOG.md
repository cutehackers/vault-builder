# Patch Log

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
