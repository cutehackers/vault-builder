# Patch Log

## 2026-07-30 - Dual-Environment Gate Correction
**Trigger:** Task 7 dual-environment gate verification.
**Scope:** Deterministic legacy fixture modes and builder-only test skip boundaries.
**Core Values Protected:** Exact migration validation and generated-Vault independence.
**Changes Applied:** Normalize legacy fixture modes from v0 catalog; run migration tests in the builder checkout and skip only absent creation-tool tests.
**Verification:** Builder and generated-Vault release gates pass.
