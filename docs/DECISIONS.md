# Decisions

## 2026-10-29 - Release Versions For vault-builder

**Release:** 1.0.0

**Question:** How should vault-builder record which build installed a vault,
when every catalog shared the fixed revision `contract-v2` and the v2 catalog
was rebuilt in place?

**Decision:** Use SemVer release versions starting at 1.0.0, kept in `VERSION`,
git tags `vX.Y.Z`, and `CHANGELOG.md`. The catalog and manifest
`builder_revision` hold the release version. Contract, manifest schema, and
vault-map versions stay separate integers, listed per release in the
changelog compatibility table. A MAJOR release is required whenever installed
vaults need manual action, including every contract bump.

**Reason:** A vault manifest must name the exact release that wrote it so that
doctor can report an outdated vault. Release and contract numbers count
different things, so they stay independent.

**Implications:**

- `docs/PUBLISH.md` is the release procedure.
- The release gate fails when `VERSION`, the latest changelog release, and the
  catalog `builder_revision` disagree.
- New entries in this file and `docs/PATCH_LOG.md` carry a `**Release:**` line.

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
