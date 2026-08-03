# Decisions

## 2026-08-03 - Defer The Full Target Gate Until Transactional Migration

**Question:** How should the local Vault transition validate its pre-apply state
when the exact version 1 tests require three catalog files that migration apply
has not created yet?

**Decision:** Before approval, run page validation, external authority scanning,
lint, health, raw-digest, no-touch, and clean-Git checks. Defer only the full
target release gate to the recoverable migration transaction, then run it again
after apply and after generated-view rebuild. Do not pre-copy managed
dependencies or weaken tests.

**Reason:** `README-kr.md`, `tools/wiki/lib/authority.py`, and
`tools/wiki/lib/vault_manifest.py` are catalog-managed migration outputs. Copying
them before audit would bypass the transaction and change the approved action
boundary, while the retained pre-apply checks still prove knowledge ownership,
raw immutability, and target cleanliness.

**Implications:**

- The approval brief must state honestly that the pre-apply full gate is
  deferred and cite the last green legacy baseline gate.
- Migration apply remains responsible for installing the complete v1 catalog
  and running the first complete-contract target gate.
- Apply must still stop on any recovery instruction, and post-apply plus
  post-generated-view full gates remain mandatory.
