# Vault Authority And Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `vault-builder` the versioned owner of Vault information roles and provide a read-only audit plus explicitly approved, rollback-safe migration path for existing Vaults.

**Architecture:** Keep durable knowledge ownership in generated Vaults while `vault-builder` owns the creation contract, managed-file catalogs, and migration engine. A new authority validator classifies Vault content without adding a mandatory `source_role` field; a builder-only migration package compares known file hashes, emits a deterministic plan, and applies only unmodified managed files through an atomic recovery journal.

**Tech Stack:** Python 3.10+ standard library, Bash, JSON, `unittest`, existing `tools/wiki` validation and release-gate commands.

## Global Constraints

- Preserve `/raw/**`, canonical user wiki bodies, `wiki/log.md`, and `/scratch/**` byte-for-byte during migration.
- `audit` performs no target writes unless the user explicitly supplies `--report`; `--plan-out` must resolve outside the target Vault.
- `apply` requires an audit-generated JSON plan and aborts before mutation when any action is `review-required` or `invalid`.
- Never follow target symlinks for reads or writes; reject path traversal and paths resolving outside the target Vault.
- Use only the Python standard library. Do not add package or network dependencies.
- The migration engine lives only in `vault-builder`; generated Vaults receive `.vault-builder/manifest.json`, not `tools/vault_migration/`.
- `init-vault.sh` and `scripts/bootstrap.sh` remain builder-only in contract version 1. New Vaults receive `scripts/release_gate.sh`; legacy copies of builder scripts are preserved and not cataloged.
- Keep optional Stenc files out of contract version 1 managed-file catalogs and preserve existing `--with-stenc` behavior.
- Do not add a mandatory `source_role` field. Classify roles from path, `type`, `canonical_source`, `generated_by`, and lifecycle fields.
- Do not automatically merge duplicate wiki pages or rebuild `wiki/index.md` and `wiki/maps/**` during migration.
- Run implementation in an isolated worktree created with `superpowers:using-git-worktrees`.
- Source design: `docs/superpowers/specs/2026-07-30-vault-authority-and-migration-design.md`.

---

## File Map

### Builder-only migration package

- Create `tools/vault_migration/__init__.py`: package marker and public version constant.
- Create `tools/vault_migration/models.py`: immutable catalog, manifest, action, and plan types with JSON serialization.
- Create `tools/vault_migration/managed_paths.py`: exact contract version 1 managed-file allowlist.
- Create `tools/vault_migration/catalogs.py`: catalog validation, hashing, and deterministic catalog generation.
- Create `tools/vault_migration/manifests.py`: generated-Vault manifest creation and loading.
- Create `tools/vault_migration/planning.py`: read-only target audit and deterministic action classification.
- Create `tools/vault_migration/applying.py`: precondition verification, staging, atomic replacement, rollback, and recovery.
- Create `tools/vault_migration/cli.py`: start with `catalog`, then extend with `manifest`, `audit`, `apply`, and `recover`.
- Create `tools/vault_migration/contracts/v0.json`: hashes from legacy builder commit `47cbf83`.
- Create `tools/vault_migration/contracts/v1.json`: hashes of the completed contract version 1 managed files.
- Create `builder_tests/__init__.py`: builder-only test package marker.
- Create `builder_tests/test_vault_migration.py`: catalog, manifest, audit, apply, rollback, recovery, and CLI tests.

### Generated-Vault authority contract

- Create `tools/wiki/lib/authority.py`: content-role classification and contract-version-aware validation.
- Modify `tools/wiki/lib/ingest.py`: create reference-only source manifests.
- Modify `tools/wiki/lib/lint.py`: run authority validation for contract version 1 Vaults.
- Modify `tools/wiki/cli.py`: include authority checks in `validate-page`.
- Modify `tests/test_wiki_tools.py`: authority, source-manifest, and generated-Vault integration coverage.

### Creation and verification

- Modify `init-vault.sh`: stop copying builder-only creation scripts and create the manifest after copying managed files.
- Modify `scripts/bootstrap.sh`: pass the builder revision into initialization.
- Modify `.gitignore`: ignore only `.vault-builder/recovery/`, while keeping the manifest trackable.
- Modify `scripts/release_gate.sh`: run builder-only migration tests when `builder_tests/` and `tools/vault_migration/` are present.
- Modify `tests/test_wiki_quality_baseline.py`: assert the release gate and authority documentation contracts.

### Documentation

- Modify `AGENTS.md`: state one-body ownership and migration safety boundaries concisely.
- Modify `README.md` and `README-kr.md`: document new-Vault manifests and existing-Vault audit/apply flow.
- Modify `docs/LLM-WIKI.md`: make information roles and builder ownership canonical.
- Modify `docs/agent/OPERATING-SCHEMA.md`: define source manifests, generated views, and reports for agents.
- Modify `docs/usage.md`: provide exact Korean migration commands and exit-code meanings.

---

### Task 1: Enforce Information Roles In Contract Version 1 Vaults

**Files:**
- Create: `tools/wiki/lib/authority.py`
- Modify: `tools/wiki/lib/ingest.py`
- Modify: `tools/wiki/lib/lint.py`
- Modify: `tools/wiki/cli.py`
- Test: `tests/test_wiki_tools.py`

**Interfaces:**
- Produces: `PageRole`, `classify_page_role(root: Path, page: Page) -> PageRole`, `authority_contract_version(root: Path) -> int | None`, `validate_authority_contract(root: Path, pages: list[Page]) -> list[Issue]`, and `validate_page_authority(root: Path, page: Page) -> list[Issue]`.
- Consumes: existing `Page`, `Issue`, `parse_page`, `lint_repo`, and source-ingest boundaries.
- Later tasks rely on `validate_authority_contract()` to block migrations with content-role violations.

- [ ] **Step 1: Write failing authority-classification and validation tests**

Add imports and focused tests to `tests/test_wiki_tools.py`:

```python
from tools.wiki.lib.authority import (
    PageRole,
    classify_page_role,
    validate_authority_contract,
)


def _write_contract_manifest(root: Path, version: int = 1) -> None:
    manifest = root / ".vault-builder" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "contract_version": version,
                "builder_revision": "test",
                "generated_at": "2026-07-30T00:00:00Z",
                "managed_files": {},
            },
            sort_keys=True,
        )
        + "\n"
    )


def _write_role_page(root: Path, relative: str, page_type: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = _frontmatter(path.stem.replace("-", " ").title(), page_type)
    body = f"# {frontmatter['title']}\n"
    if page_type == "source":
        frontmatter.update(
            {
                "canonical_source": "raw/sources/input.md",
                "raw_sha256": "a" * 64,
                "source_kind": "markdown",
                "source_count": 1,
            }
        )
        body = (
            f"# {frontmatter['title']}\n\n## Summary\n\n{frontmatter['summary']}\n\n"
            "Source: `raw/sources/input.md`\n\n"
            "## Registration\n\n"
            "- Canonical source: `raw/sources/input.md`\n"
            f"- SHA-256: `{'a' * 64}`\n"
            "- Source kind: `markdown`\n\n"
            "## Durable Knowledge Links\n\n- None.\n"
        )
    path.write_text(render_page(frontmatter, body))
    return path


class WikiAuthorityContractTest(unittest.TestCase):
    def test_classifies_source_generated_and_canonical_pages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = _write_role_page(root, "wiki/sources/input.md", "source")
            generated = _write_role_page(root, "wiki/maps/topic-map.md", "map")
            canonical = _write_role_page(root, "wiki/concepts/topic.md", "concept")

            self.assertEqual(PageRole.SOURCE_MANIFEST, classify_page_role(root, parse_page(source)))
            self.assertEqual(PageRole.GENERATED_VIEW, classify_page_role(root, parse_page(generated)))
            self.assertEqual(PageRole.CANONICAL_KNOWLEDGE, classify_page_role(root, parse_page(canonical)))

    def test_contract_v1_rejects_semantic_source_page_sections(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            _write_contract_manifest(root)
            path = _write_role_page(root, "wiki/sources/input.md", "source")
            page = parse_page(path)
            path.write_text(
                render_page(
                    page.frontmatter,
                    page.body + "\n## Key Takeaways\n\nCopied explanation.\n",
                )
            )

            issues = validate_authority_contract(root, [parse_page(path)])

            self.assertIn("source-manifest-semantic-body", {issue.code for issue in issues})

    def test_legacy_vault_does_not_enable_v1_lint_without_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            path = _write_role_page(root, "wiki/sources/input.md", "source")
            page = parse_page(path)
            path.write_text(
                render_page(
                    page.frontmatter,
                    page.body + "\n## Key Takeaways\n\nLegacy text.\n",
                )
            )

            self.assertNotIn(
                "source-manifest-semantic-body",
                {issue.code for issue in lint_repo(root).issues},
            )

    def test_malformed_manifest_is_not_treated_as_legacy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _minimal_vault(root)
            manifest = root / ".vault-builder" / "manifest.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("{not-json\n")

            self.assertIn(
                "invalid-vault-builder-manifest",
                {issue.code for issue in lint_repo(root).issues},
            )
```

Use the local `_write_role_page()` helper shown above; do not change existing
shared fixtures in a way that enables contract v1 for unrelated legacy tests.

- [ ] **Step 2: Run the focused tests and verify the missing module failure**

Run:

```bash
python3 -m unittest \
  tests.test_wiki_tools.WikiAuthorityContractTest.test_classifies_source_generated_and_canonical_pages \
  tests.test_wiki_tools.WikiAuthorityContractTest.test_contract_v1_rejects_semantic_source_page_sections \
  tests.test_wiki_tools.WikiAuthorityContractTest.test_legacy_vault_does_not_enable_v1_lint_without_manifest \
  tests.test_wiki_tools.WikiAuthorityContractTest.test_malformed_manifest_is_not_treated_as_legacy
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'tools.wiki.lib.authority'`.

- [ ] **Step 3: Implement role classification and contract-aware validation**

Create `tools/wiki/lib/authority.py` with these public types and rules:

```python
from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path

from .frontmatter import Page
from .schema import Issue


class PageRole(str, Enum):
    RAW_EVIDENCE = "raw-evidence"
    CANONICAL_KNOWLEDGE = "canonical-knowledge"
    SOURCE_MANIFEST = "source-manifest"
    GENERATED_VIEW = "generated-view"
    OPERATION_LOG = "operation-log"
    AUDIT_REPORT = "audit-report"
    OPERATING_CONTRACT = "operating-contract"


SOURCE_MANIFEST_LEVEL_TWO_HEADINGS = {
    "## Summary",
    "## Registration",
    "## Durable Knowledge Links",
}


def authority_contract_version(root: Path) -> int | None:
    path = root / ".vault-builder" / "manifest.json"
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise ValueError("Vault builder manifest must be a regular file.")
    try:
        value = json.loads(path.read_text()).get("contract_version")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Vault builder manifest is invalid.") from exc
    if not isinstance(value, int):
        raise ValueError("Vault builder manifest contract_version must be an integer.")
    return value


def classify_page_role(root: Path, page: Page) -> PageRole:
    relative = page.path.resolve().relative_to(root.resolve())
    path = relative.as_posix()
    page_type = (page.frontmatter or {}).get("type")
    if path.startswith("raw/"):
        return PageRole.RAW_EVIDENCE
    if path.startswith("scratch/reports/"):
        return PageRole.AUDIT_REPORT
    if path == "wiki/log.md" or page_type == "log":
        return PageRole.OPERATION_LOG
    if path == "wiki/index.md" or path.startswith("wiki/maps/") or page_type in {"index", "map"}:
        return PageRole.GENERATED_VIEW
    if path.startswith("wiki/sources/") or page_type == "source":
        return PageRole.SOURCE_MANIFEST
    if path.startswith("docs/") or path == "AGENTS.md":
        return PageRole.OPERATING_CONTRACT
    return PageRole.CANONICAL_KNOWLEDGE
```

Implement `validate_page_authority()` so a source manifest:

- requires `canonical_source` and `raw_sha256`;
- requires one H1 equal to frontmatter `title`, and allows only `## Summary`, `## Registration`, and `## Durable Knowledge Links`;
- rejects `## Key Takeaways`, `## Extracted Concepts`, `## Important Claims`, copied claim tables, and fenced code;
- requires frontmatter `summary` to be one line of at most 240 characters and the body summary sentence to equal it;
- requires `Source: \`<canonical_source>\`` directly below the summary so existing provenance validation remains satisfied;
- requires registration bullets for canonical source, SHA-256, and source kind;
- allows only wiki-link bullets or `- None.` below `## Durable Knowledge Links`.

Implement `validate_authority_contract()` to return no authority issues unless
`authority_contract_version(root) >= 1`. Convert a manifest `ValueError` into
one `invalid-vault-builder-manifest` error rather than silently treating it as
legacy. For enabled Vaults, validate each page and detect exact duplicate
canonical bodies by hashing body text after trimming line-end whitespace and
collapsing three-or-more blank lines to two. Exclude source manifests,
generated views, logs, and reports from duplicate-body detection. Emit
`duplicate-canonical-body` as a `review-required` error naming both relative
paths; do not merge them.

- [ ] **Step 4: Replace the source-page body template with a manifest-only body**

Change `tools/wiki/lib/ingest.py::_source_body()` to accept `source_hash` and `source_kind`, and render:

```markdown
# <source title>

## Summary

<the exact frontmatter summary>

Source: `<relative raw path>`

## Registration

- Canonical source: `<relative raw path>`
- SHA-256: `<hash>`
- Source kind: `<kind>`

## Durable Knowledge Links

- None.
```

Pass the computed hash and kind from `ingest_source()`. Remove the existing
takeaways, concepts, claim table, contradictions, pages-updated, and follow-up
sections. Set `quality.provenance` to `section` instead of `claim`. Before any
mutation, reject a supplied source summary containing a newline or more than
240 characters.

- [ ] **Step 5: Connect authority checks to lint and validate-page**

In `tools/wiki/lib/lint.py`, append `validate_authority_contract(repo_root, pages)` once after pages are parsed. In `tools/wiki/cli.py`, append `validate_page_authority(ROOT, page)` only when `authority_contract_version(ROOT) >= 1`.

Avoid importing `lint_repo` from `authority.py`; keep dependency direction `lint -> authority -> schema/frontmatter`.

- [ ] **Step 6: Run authority and existing ingest tests**

Run:

```bash
python3 -m unittest \
  tests.test_wiki_tools.WikiAuthorityContractTest \
  tests.test_wiki_tools.WikiToolCliTest.test_ingest_source_cli_registers_raw_source \
  tests.test_wiki_tools.WikiToolCliTest.test_ingest_source_is_idempotent_for_same_canonical_source_and_hash
```

Expected: all selected tests pass, and the generated source page contains `## Registration` but not `## Key Takeaways`.

- [ ] **Step 7: Commit the authority contract**

```bash
git add tools/wiki/lib/authority.py tools/wiki/lib/ingest.py tools/wiki/lib/lint.py tools/wiki/cli.py tests/test_wiki_tools.py
git commit -m "feat: enforce vault authority roles"
```

---

### Task 2: Define Managed-File Catalogs And Migration Data Contracts

**Files:**
- Create: `tools/vault_migration/__init__.py`
- Create: `tools/vault_migration/models.py`
- Create: `tools/vault_migration/managed_paths.py`
- Create: `tools/vault_migration/catalogs.py`
- Create: `tools/vault_migration/cli.py`
- Create: `tools/vault_migration/contracts/v0.json`
- Create: `tools/vault_migration/contracts/v1.json`
- Create: `builder_tests/__init__.py`
- Create: `builder_tests/test_vault_migration.py`

**Interfaces:**
- Produces: `ManagedFile`, `ContractCatalog`, `VaultManifest`, `MigrationAction`, `MigrationPlan`, `sha256_file(path: Path) -> str`, `load_catalog(path: Path) -> ContractCatalog`, `build_catalog(builder_root: Path, contract_version: int, revision: str, source_ref: str | None = None) -> ContractCatalog`, and the initial `catalog` CLI command.
- Consumes: the exact `MANAGED_PATHS` allowlist.
- Later tasks rely on canonical JSON rendering through each model's `to_dict()` and parser classmethod.

- [ ] **Step 1: Write failing catalog and model tests**

Create `builder_tests/test_vault_migration.py`:

```python
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.vault_migration.catalogs import load_catalog, sha256_file
from tools.vault_migration.managed_paths import MANAGED_PATHS
from tools.vault_migration.models import MigrationAction, MigrationPlan


ROOT = Path(__file__).resolve().parents[1]


class ContractCatalogTest(unittest.TestCase):
    def test_v1_catalog_matches_current_managed_files(self):
        catalog = load_catalog(ROOT / "tools" / "vault_migration" / "contracts" / "v1.json")

        self.assertEqual(1, catalog.contract_version)
        self.assertEqual(set(MANAGED_PATHS), set(catalog.managed_files))
        for relative, managed in catalog.managed_files.items():
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            self.assertFalse(path.is_symlink(), relative)
            self.assertEqual(managed.sha256, sha256_file(path), relative)

    def test_plan_json_round_trip_is_canonical(self):
        action = MigrationAction(
            path="tools/wiki/cli.py",
            action="replace",
            current_sha256="a" * 64,
            desired_sha256="b" * 64,
            current_mode="0644",
            desired_mode="0644",
            reason="known version 0 managed file",
        )
        plan = MigrationPlan(
            schema_version=1,
            migration_id="0123456789abcdef",
            current_contract_version=None,
            target_contract_version=1,
            builder_revision="test",
            target_fingerprint="c" * 64,
            actions=(action,),
            authority_issues=(),
        )

        rendered = plan.to_json()

        self.assertEqual(plan, MigrationPlan.from_json(rendered))
        self.assertEqual(rendered, json.dumps(plan.to_dict(), indent=2, sort_keys=True) + "\n")
```

- [ ] **Step 2: Run the model tests and verify they fail**

Run:

```bash
python3 -m unittest builder_tests.test_vault_migration.ContractCatalogTest
```

Expected: `ERROR` because `tools.vault_migration` does not exist.

- [ ] **Step 3: Implement immutable migration models**

Use frozen dataclasses and tuples in `models.py`. Validate at parse boundaries:

```python
@dataclass(frozen=True)
class ManagedFile:
    path: str
    sha256: str
    mode: str
    policy: str = "replace-if-unmodified"


@dataclass(frozen=True)
class ContractCatalog:
    schema_version: int
    contract_version: int
    builder_revision: str
    managed_files: dict[str, ManagedFile]


@dataclass(frozen=True)
class VaultManifest:
    schema_version: int
    contract_version: int
    builder_revision: str
    generated_at: str
    managed_files: dict[str, ManagedFile]
    last_migration_id: str | None = None


MigrationActionName = Literal[
    "unchanged",
    "create",
    "replace",
    "preserve",
    "review-required",
    "rebuild-suggested",
    "invalid",
]


@dataclass(frozen=True)
class MigrationAction:
    path: str
    action: MigrationActionName
    current_sha256: str | None
    desired_sha256: str | None
    current_mode: str | None
    desired_mode: str | None
    reason: str


@dataclass(frozen=True)
class MigrationPlan:
    schema_version: int
    migration_id: str
    current_contract_version: int | None
    target_contract_version: int
    builder_revision: str
    target_fingerprint: str
    actions: tuple[MigrationAction, ...]
    authority_issues: tuple[dict[str, object], ...]
```

Add:

```python
@property
def has_blocking_actions(self) -> bool:
    return any(action.action in {"review-required", "invalid"} for action in self.actions)
```

All `from_dict()` methods must reject unknown keys, non-integer versions,
migration IDs other than 16 lowercase hexadecimal characters, absolute paths,
`..` path segments, non-64-character lowercase hexadecimal hashes, modes
outside `{"0644", "0755"}`, and unknown policies/actions. JSON output must use
`indent=2`, `sort_keys=True`, and one trailing newline.

- [ ] **Step 4: Define the exact managed-file allowlist**

In `managed_paths.py`, define a sorted tuple built from explicit roots:

```python
MANAGED_EXACT_PATHS = (
    ".agents/skills/wiki-ingest/SKILL.md",
    ".agents/skills/wiki-query/SKILL.md",
    ".agents/skills/wiki-update/SKILL.md",
    ".github/workflows/release-gate.yml",
    ".gitignore",
    "AGENTS.md",
    "README-kr.md",
    "README.md",
    "docs/LLM-WIKI.md",
    "docs/USER_PROMPTS.md",
    "docs/VAULT_PROMPTS.md",
    "docs/agent/DRAFTS.md",
    "docs/agent/OPERATING-SCHEMA.md",
    "docs/usage.md",
    "scripts/release_gate.sh",
    "tests/__init__.py",
    "tests/test_wiki_quality_baseline.py",
    "tests/test_wiki_tools.py",
)

MANAGED_TREE_ROOTS = ("tools/wiki",)
```

Build `MANAGED_PATHS` by enumerating regular non-symlink files beneath `tools/wiki` in the builder checkout at catalog-generation time, excluding `__pycache__`, `.pyc`, `.DS_Store`, and temporary files. Never include `wiki/**`, `raw/**`, `scratch/**`, `docs/stenc/**`, `tools/stenc/**`, `tools/vault_migration/**`, `builder_tests/**`, or `.vault-builder/**`.

- [ ] **Step 5: Implement catalog loading and deterministic generation**

In `catalogs.py`:

- hash files in binary mode with SHA-256;
- record executable files as `"0755"` and all others as `"0644"`;
- reject symlinks;
- for a worktree catalog, read the current filesystem;
- for `source_ref="47cbf83"`, read bytes and mode with `git show <ref>:<path>` and `git ls-tree`;
- omit managed files that do not exist in the legacy ref;
- write catalogs only through `write_catalog(path, catalog)`, using canonical JSON.

The runtime `audit` and `apply` paths only load committed catalogs; they never invoke Git.

- [ ] **Step 6: Generate and verify version 0 and version 1 catalogs**

Create `tools/vault_migration/cli.py` with the builder-root `sys.path` bootstrap
used by `tools/wiki/cli.py`. Add only this command in this task:

```bash
python3 tools/vault_migration/cli.py catalog \
  --contract-version 0 \
  --revision 47cbf83 \
  --source-ref 47cbf83 \
  --output tools/vault_migration/contracts/v0.json

python3 tools/vault_migration/cli.py catalog \
  --contract-version 1 \
  --revision contract-v1 \
  --output tools/vault_migration/contracts/v1.json
```

Run:

```bash
python3 -m unittest builder_tests.test_vault_migration.ContractCatalogTest
```

Expected: all catalog and model tests pass. Confirm `v0.json` contains no Task 1-only files that were absent at commit `47cbf83`, while `v1.json` exactly matches `MANAGED_PATHS`.

- [ ] **Step 7: Commit the migration contracts**

```bash
git add tools/vault_migration builder_tests
git commit -m "feat: define vault migration contracts"
```

---

### Task 3: Generate Versioned Manifests For New Vaults

**Files:**
- Create: `tools/vault_migration/manifests.py`
- Modify: `tools/vault_migration/cli.py`
- Modify: `init-vault.sh`
- Modify: `scripts/bootstrap.sh`
- Modify: `.gitignore`
- Modify: `tests/test_wiki_tools.py`
- Modify: `builder_tests/test_vault_migration.py`
- Modify: `tools/vault_migration/contracts/v1.json`

**Interfaces:**
- Produces: `create_manifest(builder_root: Path, target_root: Path, builder_revision: str, generated_at: datetime | None = None) -> VaultManifest`, `load_manifest(target_root: Path) -> VaultManifest | None`, and `write_manifest(target_root: Path, manifest: VaultManifest) -> Path`.
- Consumes: version 1 `ContractCatalog`.
- Later audit uses the generated manifest as the first source of prior-file hashes.

- [ ] **Step 1: Write failing manifest and init integration tests**

Add to `builder_tests/test_vault_migration.py`:

```python
class VaultManifestTest(unittest.TestCase):
    def test_manifest_records_only_copied_managed_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            result = subprocess.run(
                ["bash", "init-vault.sh", str(target)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "VAULT_BUILDER_REVISION": "test-revision"},
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            manifest = load_manifest(target)
            self.assertIsNotNone(manifest)
            self.assertEqual(1, manifest.contract_version)
            self.assertEqual("test-revision", manifest.builder_revision)
            self.assertNotIn("wiki/overview.md", manifest.managed_files)
            self.assertNotIn("tools/vault_migration/cli.py", manifest.managed_files)
            for relative, managed in manifest.managed_files.items():
                self.assertEqual(managed.sha256, sha256_file(target / relative), relative)
```

Extend `tests/test_wiki_tools.py::test_init_vault_creates_usable_environment` to assert:

```python
manifest = json.loads((target / ".vault-builder" / "manifest.json").read_text())
self.assertEqual(1, manifest["contract_version"])
self.assertIn("tools/wiki/cli.py", manifest["managed_files"])
self.assertNotIn("wiki/overview.md", manifest["managed_files"])
self.assertFalse((target / "tools" / "vault_migration").exists())
self.assertFalse((target / "init-vault.sh").exists())
self.assertFalse((target / "scripts" / "bootstrap.sh").exists())
self.assertTrue((target / "scripts" / "release_gate.sh").exists())
```

Remove `target / "scripts" / "bootstrap.sh"` from that test's
`expected_files`, then keep the explicit absence assertion above. In
`test_init_vault_uses_vault_as_default_target_directory`, replace the current
bootstrap existence assertion with assertions that the manifest and release
gate exist while the local init and bootstrap scripts do not.

- [ ] **Step 2: Run the new tests and verify manifest absence**

Run:

```bash
python3 -m unittest \
  builder_tests.test_vault_migration.VaultManifestTest \
  tests.test_wiki_tools.WikiToolCliTest.test_init_vault_creates_usable_environment
```

Expected: fail because `.vault-builder/manifest.json` is missing.

- [ ] **Step 3: Implement manifest creation and loading**

`create_manifest()` must:

- load `contracts/v1.json`;
- include only catalog paths that exist as regular non-symlink files in the target;
- verify each copied file hash equals the desired catalog hash;
- use `VAULT_BUILDER_REVISION`, or `git rev-parse HEAD`, or `"contract-v1"` in that order;
- format timestamps as UTC `YYYY-MM-DDTHH:MM:SSZ`;
- return the immutable `VaultManifest` without writing.

`write_manifest()` must reject a symlinked `.vault-builder` directory or
manifest and write `.vault-builder/manifest.json` through a sibling temporary
file plus `os.replace`. `load_manifest()` returns `None` only when the file is
absent. Malformed or unsafe manifests raise `ValueError`.

- [ ] **Step 4: Add the manifest CLI command**

Extend the existing parser in `tools/vault_migration/cli.py`:

```python
manifest = subcommands.add_parser("manifest")
manifest.add_argument("--builder-root", required=True)
manifest.add_argument("--target", required=True)
manifest.add_argument("--builder-revision", required=True)
```

The handler calls `create_manifest()` and `write_manifest()`, prints the
manifest path and contract version, and returns 1 on `OSError` or `ValueError`.

- [ ] **Step 5: Integrate manifest generation into init and bootstrap**

In `init-vault.sh`, remove `copy_file "init-vault.sh"` and replace
`copy_tree "scripts"` with `copy_file "scripts/release_gate.sh"`. Do not copy
`scripts/bootstrap.sh` into the target. Select Python using the same Python
3.10+ loop as `scripts/bootstrap.sh`, then run after all managed files have
been copied and chmodded:

```bash
"${PYTHON_BIN}" "${SCRIPT_DIR}/tools/vault_migration/cli.py" manifest \
  --builder-root "${SCRIPT_DIR}" \
  --target "${TARGET_DIR}" \
  --builder-revision "${VAULT_BUILDER_REVISION:-contract-v1}"
```

In `scripts/bootstrap.sh`, invoke initialization as:

```bash
VAULT_BUILDER_REVISION="${REF}" \
  PYTHON="${PYTHON_BIN}" \
  bash "${template_dir}/init-vault.sh" "${init_args[@]}"
```

Add only this recovery path to `.gitignore`:

```gitignore
.vault-builder/recovery/
```

Do not ignore `.vault-builder/manifest.json`. Update initialization tests and
architecture docs that previously treated the creation scripts as part of the
generated Vault topology. Existing legacy copies are outside the v1 managed
allowlist, so audit preserves them and apply never deletes them.

- [ ] **Step 6: Regenerate v1 catalog after managed-file changes**

Run the worktree catalog generation snippet from Task 2 for `v1.json`, then:

```bash
python3 -m unittest \
  builder_tests.test_vault_migration.ContractCatalogTest \
  builder_tests.test_vault_migration.VaultManifestTest \
  tests.test_wiki_tools.WikiToolCliTest.test_init_vault_creates_usable_environment \
  tests.test_wiki_tools.WikiToolCliTest.test_init_vault_can_include_stenc_when_requested
```

Expected: all selected tests pass for default and Stenc-enabled initialization.

- [ ] **Step 7: Commit new-Vault manifests**

```bash
git add .gitignore init-vault.sh scripts/bootstrap.sh tools/vault_migration/manifests.py tools/vault_migration/contracts/v1.json builder_tests/test_vault_migration.py tests/test_wiki_tools.py
git commit -m "feat: record generated vault contracts"
```

---

### Task 4: Build The Read-Only Legacy Vault Audit Planner

**Files:**
- Create: `tools/vault_migration/planning.py`
- Modify: `tools/vault_migration/models.py`
- Modify: `builder_tests/test_vault_migration.py`

**Interfaces:**
- Produces: `audit_vault(builder_root: Path, target_root: Path) -> MigrationPlan`, `target_fingerprint(target_root: Path, paths: Iterable[str]) -> str`, and `render_plan_summary(target_root: Path, plan: MigrationPlan) -> str`.
- Consumes: v0/v1 catalogs, optional target manifest, `validate_authority_contract()` in forced audit mode, and target file hashes/modes.
- Later apply consumes the exact action order and target fingerprint from this plan.

- [ ] **Step 1: Write failing read-only planning tests**

Add tests that create a minimal legacy target with one v0-known managed file, one customized managed file, one missing v1 file, raw content, canonical wiki content, log, report, and generated map:

```python
class MigrationPlanningTest(unittest.TestCase):
    def test_audit_is_read_only_and_classifies_known_custom_and_owned_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_legacy_fixture(ROOT, target)
            (target / "tools" / "wiki" / "cli.py").write_text("user customization\n")
            before = _tree_digest(target)

            plan = audit_vault(ROOT, target)

            self.assertEqual(before, _tree_digest(target))
            actions = {action.path: action for action in plan.actions}
            self.assertEqual("review-required", actions["tools/wiki/cli.py"].action)
            self.assertEqual("preserve", actions["raw/sources/input.md"].action)
            self.assertEqual("preserve", actions["wiki/concepts/topic.md"].action)
            self.assertEqual("preserve", actions["wiki/log.md"].action)
            self.assertEqual("preserve", actions["scratch/reports/audit.md"].action)
            self.assertEqual("rebuild-suggested", actions["wiki/maps/topic-map.md"].action)

    def test_audit_rejects_target_symlink_and_plan_output_inside_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vault"
            alias = root / "vault-link"
            _copy_clean_legacy_fixture(ROOT, target)
            alias.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "target root must not be a symlink"):
                audit_vault(ROOT, alias)
            with self.assertRaisesRegex(ValueError, "must stay outside the target Vault"):
                validate_external_output_path(target, target / "migration-plan.json")
```

Implement `_tree_digest()` from sorted relative path, file type, mode, and file SHA-256 so the test detects created, deleted, or modified files.

- [ ] **Step 2: Run planning tests and verify the missing planner failure**

Run:

```bash
python3 -m unittest builder_tests.test_vault_migration.MigrationPlanningTest
```

Expected: `ERROR` because `planning.py` does not exist.

- [ ] **Step 3: Implement topology and path-safety validation**

Recognize a Vault root only when all are regular, non-symlink paths:

```text
AGENTS.md
wiki/index.md
wiki/log.md
tools/wiki/cli.py
scripts/release_gate.sh
```

Reject a target root that is itself a symlink. For every inspected relative path:

1. reject absolute and `..` paths;
2. use `lstat()` before `resolve()`;
3. reject symlink components from target root to leaf;
4. verify the resolved parent remains under `target_root.resolve()`.

Keep these checks in private helpers inside `planning.py`; Task 5 imports the same helpers rather than reimplementing them.

- [ ] **Step 4: Implement deterministic action classification**

For sorted v1 catalog paths:

- `unchanged`: target hash equals desired v1 hash;
- `create`: target path is absent;
- `replace`: target hash equals the manifest hash or any same-path hash in v0 catalog;
- `review-required`: target regular file exists but its hash is unknown;
- `invalid`: target path is a symlink, directory, unreadable, or escapes the root.

Append sorted non-catalog role actions:

- `preserve` for all regular files below `raw/`, canonical `wiki/` pages, `wiki/log.md`, and `scratch/`;
- `preserve` for legacy `init-vault.sh` and `scripts/bootstrap.sh`, with the
  reason that creation is now builder-only and these copies are not executed or
  deleted by migration;
- `rebuild-suggested` for `wiki/index.md` and `wiki/maps/*.md`;
- `review-required` for authority violations and exact duplicate canonical bodies.

Use a forced validation entrypoint `scan_authority_issues(root, pages)` that runs regardless of manifest version; keep `validate_authority_contract()` version-gated for normal legacy lint.

Record the manifest version as `current_contract_version`, using `None` for a
legacy Vault. Compute `migration_id` as the first 16 characters of SHA-256 over
canonical JSON containing current and target contract versions, builder
revision, target fingerprint, actions, and authority issues. Do not use time or
randomness.

- [ ] **Step 5: Add plan-output path safety**

Implement:

```python
def validate_external_output_path(target_root: Path, output_path: Path) -> None:
    target = target_root.resolve()
    output = output_path.resolve(strict=False)
    try:
        output.relative_to(target)
    except ValueError:
        return
    raise ValueError("Migration plan output must stay outside the target Vault.")
```

Reject symlinked output parents and create only the final parent directories outside the target.

- [ ] **Step 6: Run planning tests**

Run:

```bash
python3 -m unittest builder_tests.test_vault_migration.MigrationPlanningTest
```

Expected: all planning tests pass and the read-only tree digest remains identical.

- [ ] **Step 7: Commit the audit planner**

```bash
git add tools/vault_migration/planning.py tools/vault_migration/models.py builder_tests/test_vault_migration.py
git commit -m "feat: add read-only vault migration audit"
```

---

### Task 5: Apply Plans Atomically And Recover Failures

**Files:**
- Create: `tools/vault_migration/applying.py`
- Modify: `tools/vault_migration/manifests.py`
- Modify: `builder_tests/test_vault_migration.py`

**Interfaces:**
- Produces: `apply_plan(builder_root: Path, target_root: Path, plan: MigrationPlan, run_gate: Callable[[Path], None] | None = None) -> VaultManifest` and `recover_migration(target_root: Path, migration_id: str) -> None`.
- Consumes: the plan fingerprint, v1 catalog, safe-path helpers, manifest writer, and target release gate.
- Later CLI maps `ValueError`, `OSError`, and `subprocess.CalledProcessError` to stable exit codes.

- [ ] **Step 1: Write failing apply, rollback, and recovery tests**

Add:

```python
class MigrationApplyTest(unittest.TestCase):
    def test_apply_replaces_only_approved_managed_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_clean_legacy_fixture(ROOT, target)
            protected_before = _protected_hashes(target)
            plan = audit_vault(ROOT, target)

            manifest = apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertEqual(1, manifest.contract_version)
            self.assertEqual(protected_before, _protected_hashes(target))
            self.assertFalse((target / ".vault-builder" / "recovery" / plan.migration_id).exists())

    def test_apply_aborts_without_writes_when_review_is_required(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_clean_legacy_fixture(ROOT, target)
            (target / "tools" / "wiki" / "cli.py").write_text("user customization\n")
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)

            with self.assertRaisesRegex(ValueError, "requires human review"):
                apply_plan(ROOT, target, plan, run_gate=lambda _: None)

            self.assertEqual(before, _tree_digest(target))

    def test_apply_rolls_back_when_release_gate_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_clean_legacy_fixture(ROOT, target)
            before = _tree_digest(target)
            plan = audit_vault(ROOT, target)

            def fail_gate(_: Path) -> None:
                raise subprocess.CalledProcessError(1, ["scripts/release_gate.sh"])

            with self.assertRaises(subprocess.CalledProcessError):
                apply_plan(ROOT, target, plan, run_gate=fail_gate)

            self.assertEqual(before, _tree_digest(target, exclude_recovery=True))

    def test_recover_restores_interrupted_journal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_clean_legacy_fixture(ROOT, target)
            relative = Path("tools/wiki/cli.py")
            original = (target / relative).read_bytes()
            original_hash = hashlib.sha256(original).hexdigest()
            migration_id = "0123456789abcdef"
            journal_root = target / ".vault-builder" / "recovery" / migration_id
            original_path = journal_root / "originals" / relative
            original_path.parent.mkdir(parents=True, exist_ok=True)
            original_path.write_bytes(original)
            (target / relative).write_text("interrupted replacement\n")
            (journal_root / "journal.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "migration_id": migration_id,
                        "target_contract_version": 1,
                        "state": "applying",
                        "entries": [
                            {
                                "path": relative.as_posix(),
                                "existed": True,
                                "sha256": original_hash,
                                "mode": "0755",
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )

            recover_migration(target, migration_id)

            self.assertEqual(original, (target / relative).read_bytes())
            self.assertFalse(journal_root.exists())
```

`_protected_hashes()` must include every regular file below `raw/`, `wiki/`, and `scratch/`, not only one sample.

- [ ] **Step 2: Run apply tests and verify the missing engine failure**

Run:

```bash
python3 -m unittest builder_tests.test_vault_migration.MigrationApplyTest
```

Expected: `ERROR` because `applying.py` does not exist.

- [ ] **Step 3: Implement precondition verification and all-or-nothing refusal**

Before creating `.vault-builder/recovery/`:

- parse and validate the plan again;
- require `target_contract_version == 1`;
- reject any `review-required` or `invalid` action;
- recompute the target fingerprint and require equality;
- verify every desired builder file still matches the plan's desired hash and mode;
- verify every current target file matches the action's current hash and mode;
- verify there is no existing recovery journal for the same migration ID.

Any mismatch raises `ValueError("Migration target changed after audit; run audit again.")` without target writes.

- [ ] **Step 4: Implement the recovery journal**

Journal layout:

```text
.vault-builder/recovery/<migration-id>/
├── journal.json
└── originals/
    └── <relative managed paths>
```

`journal.json` records, in sorted action order:

```json
{
  "schema_version": 1,
  "migration_id": "<id>",
  "target_contract_version": 1,
  "state": "prepared",
  "entries": [
    {
      "path": "tools/wiki/cli.py",
      "existed": true,
      "sha256": "<original hash>",
      "mode": "0755"
    }
  ]
}
```

Write original bytes and modes before replacing any target. Write journal states through atomic replace: `prepared`, `applying`, `validating`, `rolling-back`, `rollback-failed`, or `complete`.

- [ ] **Step 5: Implement atomic managed-file replacement**

For each `create` or `replace` action:

1. ensure the target parent has no symlink component;
2. create a named temporary file in the target parent;
3. copy builder bytes, flush, and `os.fsync()`;
4. apply the desired mode;
5. verify the temporary file hash;
6. call `os.replace(temp_path, target_path)`;
7. fsync the parent directory where supported.

Do not touch `unchanged`, `preserve`, or `rebuild-suggested` actions.

- [ ] **Step 6: Validate, write manifest, and roll back on failure**

The default gate runner is:

```python
def _run_release_gate(target_root: Path) -> None:
    subprocess.run(
        ["bash", "scripts/release_gate.sh"],
        cwd=target_root,
        text=True,
        check=True,
    )
```

After file replacement:

1. run the release gate, which already runs lint, health, maps check, and metrics;
2. create the version 1 manifest with `last_migration_id`;
3. mark the journal complete;
4. remove only `.vault-builder/recovery/<migration-id>`.

On any exception after journal creation, restore originals in reverse action order, delete files whose entry has `existed: false`, restore modes, verify hashes, and remove the journal only when recovery is verified. If recovery fails, preserve the journal as `rollback-failed` and raise an error containing the exact `recover` command.

- [ ] **Step 7: Implement explicit interrupted recovery**

`recover_migration()` must:

- validate the migration ID as lowercase hexadecimal, 16 characters;
- refuse journals marked `complete`;
- restore only journal-listed relative managed paths;
- reject changed journal originals and unsafe target paths;
- be idempotent after a successful restore by returning a clear “journal not found” error;
- never read desired files from the builder checkout.

- [ ] **Step 8: Run apply and recovery tests**

Run:

```bash
python3 -m unittest builder_tests.test_vault_migration.MigrationApplyTest
```

Expected: all apply, precondition, rollback, and recovery tests pass; protected hashes remain identical.

- [ ] **Step 9: Commit atomic migration apply**

```bash
git add tools/vault_migration/applying.py tools/vault_migration/manifests.py builder_tests/test_vault_migration.py
git commit -m "feat: apply vault migrations atomically"
```

---

### Task 6: Expose The Builder-Only Migration CLI

**Files:**
- Modify: `tools/vault_migration/cli.py`
- Modify: `builder_tests/test_vault_migration.py`
- Modify: `tools/vault_migration/contracts/v1.json`

**Interfaces:**
- Produces these exact commands:
  - `python3 tools/vault_migration/cli.py catalog --contract-version N --revision VALUE [--source-ref REF] --output PATH`
  - `python3 tools/vault_migration/cli.py manifest --builder-root PATH --target PATH --builder-revision VALUE`
  - `python3 tools/vault_migration/cli.py audit --target PATH [--plan-out PATH] [--report PATH]`
  - `python3 tools/vault_migration/cli.py apply --target PATH --plan PATH`
  - `python3 tools/vault_migration/cli.py recover --target PATH --migration-id ID`
- Consumes: all Task 2–5 service functions.

- [ ] **Step 1: Write failing CLI tests**

Add subprocess tests:

```python
class VaultMigrationCliTest(unittest.TestCase):
    def test_audit_stdout_is_deterministic_and_target_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            plan_path = Path(tmpdir) / "plan.json"
            _copy_clean_legacy_fixture(ROOT, target)
            before = _tree_digest(target)

            first = _run_cli("audit", "--target", str(target), "--plan-out", str(plan_path))
            second = _run_cli("audit", "--target", str(target))

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual(before, _tree_digest(target))
            self.assertEqual(MigrationPlan.from_json(plan_path.read_text()).to_json(), plan_path.read_text())

    def test_audit_returns_two_for_review_required(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_clean_legacy_fixture(ROOT, target)
            (target / "tools" / "wiki" / "cli.py").write_text("user customization\n")

            result = _run_cli("audit", "--target", str(target))

            self.assertEqual(2, result.returncode, result.stderr)
            self.assertIn("review-required: 1", result.stdout)
            self.assertFalse((target / ".vault-builder").exists())

    def test_apply_requires_plan_and_never_accepts_implicit_yes(self):
        result = _run_cli("apply", "--target", "/tmp/example")
        self.assertEqual(1, result.returncode)
        self.assertIn("--plan", result.stderr)

    def test_recover_prints_actionable_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "vault"
            _copy_clean_legacy_fixture(ROOT, target)

            result = _run_cli(
                "recover",
                "--target",
                str(target),
                "--migration-id",
                "0123456789abcdef",
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("Recovery journal not found", result.stderr)
```

- [ ] **Step 2: Run CLI tests and verify the missing adapter failure**

Run:

```bash
python3 -m unittest builder_tests.test_vault_migration.VaultMigrationCliTest
```

Expected: fail because the existing catalog-only CLI does not yet define
`audit`, `apply`, or `recover`.

- [ ] **Step 3: Implement argparse commands and stable exit codes**

At file start, add builder root to `sys.path` using the same pattern as `tools/wiki/cli.py`.

Exit codes:

- `0`: command completed; audit has no `review-required` or `invalid` actions;
- `1`: invalid arguments, unsafe path, malformed input, apply failure, gate failure, or recovery failure;
- `2`: audit completed read-only but requires human review.

For `audit`:

- print `render_plan_summary(target_root, plan)` followed by canonical plan JSON;
- write `--plan-out` only after `validate_external_output_path()`;
- allow `--report` only under `<target>/scratch/reports/`, reject symlinked report roots, and state clearly that this explicit option writes to the target;
- return 2 when the plan contains `review-required`, but still write the external plan.

For `apply`, require a regular non-symlink plan file, parse it before any target access, and never support `--yes`, `--force`, partial apply, or implicit audit.

- [ ] **Step 4: Add human-readable rendering**

`render_plan_summary(target_root, plan)` output must include:

```text
Target: /absolute/path
Current contract: legacy | <number>
Target contract: 1
Migration ID: <id>
Actions:
  unchanged: <count>
  create: <count>
  replace: <count>
  preserve: <count>
  review-required: <count>
  rebuild-suggested: <count>
  invalid: <count>
Target writes: none
```

For apply and recover, print changed paths and final contract version without printing file contents.

- [ ] **Step 5: Regenerate v1 catalog and run CLI tests**

Regenerate `contracts/v1.json`, then run:

```bash
python3 -m unittest \
  builder_tests.test_vault_migration.ContractCatalogTest \
  builder_tests.test_vault_migration.VaultMigrationCliTest
```

Expected: all tests pass; two identical audits produce identical stdout and plan JSON.

- [ ] **Step 6: Commit the CLI**

```bash
git add tools/vault_migration/cli.py tools/vault_migration/contracts/v1.json builder_tests/test_vault_migration.py
git commit -m "feat: expose vault migration workflow"
```

---

### Task 7: Wire Builder Verification And User Documentation

**Files:**
- Modify: `scripts/release_gate.sh`
- Modify: `tests/test_wiki_quality_baseline.py`
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `README-kr.md`
- Modify: `docs/LLM-WIKI.md`
- Modify: `docs/agent/OPERATING-SCHEMA.md`
- Modify: `docs/usage.md`
- Modify: `tools/vault_migration/contracts/v1.json`

**Interfaces:**
- Consumes: final CLI commands and role terminology.
- Produces: one canonical user workflow and one release gate for both the builder checkout and generated Vaults.

- [ ] **Step 1: Write failing release-gate and documentation contract tests**

In `tests/test_wiki_quality_baseline.py`, assert:

```python
def test_authority_and_migration_docs_use_the_public_contract(self):
    docs = "\n".join(
        (ROOT / relative).read_text()
        for relative in [
            "AGENTS.md",
            "README.md",
            "README-kr.md",
            "docs/LLM-WIKI.md",
            "docs/agent/OPERATING-SCHEMA.md",
            "docs/usage.md",
        ]
    )
    for token in [
        "One Body Owner",
        ".vault-builder/manifest.json",
        "tools/vault_migration/cli.py audit",
        "tools/vault_migration/cli.py apply",
        "review-required",
        "Vault Graph",
    ]:
        self.assertIn(token, docs)


def test_release_gate_runs_builder_tests_only_when_available(self):
    text = (ROOT / "scripts" / "release_gate.sh").read_text()
    self.assertIn("builder_tests/test_vault_migration.py", text)
    self.assertIn('if [ -f "builder_tests/test_vault_migration.py" ]', text)
```

Keep the existing concise `AGENTS.md` limit at 180 lines.

- [ ] **Step 2: Run the documentation tests and verify they fail**

Run:

```bash
python3 -m unittest \
  tests.test_wiki_quality_baseline.WikiQualityBaselineTest.test_authority_and_migration_docs_use_the_public_contract \
  tests.test_wiki_quality_baseline.WikiQualityBaselineTest.test_release_gate_runs_builder_tests_only_when_available
```

Expected: fail on missing documentation tokens and builder-test gate.

- [ ] **Step 3: Extend the release gate without breaking generated Vaults**

Build the test command as a Bash array:

```bash
wiki_tests=(
  tests/test_wiki_quality_baseline.py
  tests/test_wiki_tools.py
)
if [ -f "builder_tests/test_vault_migration.py" ] && [ -d "tools/vault_migration" ]; then
  wiki_tests+=(builder_tests/test_vault_migration.py)
fi

run "$PYTHON_BIN -m unittest ${wiki_tests[*]}" \
  "$PYTHON_BIN" -m unittest "${wiki_tests[@]}"
```

Update the existing baseline token assertion from one exact two-file command to the array tokens. A generated Vault has no `builder_tests/` or `tools/vault_migration/`, so its copied gate continues to run only the existing wiki tests.

- [ ] **Step 4: Update canonical architecture and agent contracts**

In `docs/LLM-WIKI.md`, add:

- the English heading `One Body Owner, Many References`;
- the role table from the approved design;
- `vault-builder` as structure and migration owner;
- generated Vault as raw and durable wiki owner;
- Vault Graph as read-only consumer;
- manifest as operational metadata, not knowledge.
- creation and migration scripts as builder-only tools; generated Vaults keep
  the release gate but do not become nested Vault builders.

In `docs/agent/OPERATING-SCHEMA.md`, state:

- source pages contain registration metadata and durable-page links only;
- canonical pages own reusable explanations;
- generated views and reports are not durable claim authorities;
- agents never edit a manifest or run apply implicitly.

In `AGENTS.md`, add no more than 12 lines covering the one-body rule, source manifests, and explicit migration boundary.

- [ ] **Step 5: Document exact user workflows in English and Korean**

Add these commands to `README.md`, `README-kr.md`, and `docs/usage.md`:

```bash
cd /path/to/vault-builder

python3 tools/vault_migration/cli.py audit \
  --target /path/to/vault \
  --plan-out /tmp/vault-migration-plan.json

python3 tools/vault_migration/cli.py apply \
  --target /path/to/vault \
  --plan /tmp/vault-migration-plan.json
```

Document exit codes 0, 1, and 2; state that exit 2 is a successful read-only audit requiring review, not a tool crash. State that users must inspect the plan before apply and that apply does not merge wiki knowledge or rebuild navigation.

- [ ] **Step 6: Regenerate v1 catalog and run baseline tests**

Regenerate `contracts/v1.json`, then run:

```bash
python3 -m unittest \
  tests.test_wiki_quality_baseline \
  builder_tests.test_vault_migration.ContractCatalogTest
```

Expected: all baseline and catalog-integrity tests pass; `AGENTS.md` remains at or below 180 lines.

- [ ] **Step 7: Commit verification and docs**

```bash
git add scripts/release_gate.sh tests/test_wiki_quality_baseline.py AGENTS.md README.md README-kr.md docs/LLM-WIKI.md docs/agent/OPERATING-SCHEMA.md docs/usage.md tools/vault_migration/contracts/v1.json
git commit -m "docs: publish vault migration contract"
```

---

### Task 8: Prove Fresh Creation, Legacy Migration, Rollback, And Real-Vault Audit

**Files:**
- Modify: `builder_tests/test_vault_migration.py`
- Modify: `tools/vault_migration/contracts/v1.json`

**Interfaces:**
- Consumes: the entire Round 0-VB public contract.
- Produces: final acceptance evidence without applying changes to `/Users/junhyounglee/vault`.

- [ ] **Step 1: Add an end-to-end migration acceptance test**

Create a temporary legacy Vault from commit `47cbf83` by archiving that revision and running its `init-vault.sh`. Then add representative user files under raw, wiki, and scratch. Audit with the current builder, apply the plan, and assert:

```python
class VaultMigrationAcceptanceTest(unittest.TestCase):
    def test_legacy_vault_migrates_without_changing_user_knowledge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            legacy_builder = _extract_git_archive(ROOT, "47cbf83", root / "legacy-builder")
            target = root / "vault"
            _run(["bash", "init-vault.sh", str(target)], cwd=legacy_builder)
            _add_user_knowledge(target)
            protected_before = _protected_hashes(target)

            plan = audit_vault(ROOT, target)
            self.assertFalse(plan.has_blocking_actions)
            apply_plan(ROOT, target, plan)

            self.assertEqual(protected_before, _protected_hashes(target))
            self.assertEqual(1, load_manifest(target).contract_version)
            _run(["bash", "scripts/release_gate.sh"], cwd=target)
```

If legacy seed source pages violate the new source-manifest contract, keep the acceptance fixture source-free. A separate audit test must prove such a source page becomes `review-required` without modification.

- [ ] **Step 2: Run the acceptance test and correct only contract mismatches**

Run:

```bash
python3 -m unittest builder_tests.test_vault_migration.VaultMigrationAcceptanceTest
```

Expected: the temporary version 0 Vault migrates to version 1, protected hashes remain identical, and the migrated Vault release gate passes.

If the test finds a mismatch, correct the migration implementation or catalog. Do not weaken protected-path assertions or add force/partial-apply behavior.

- [ ] **Step 3: Regenerate the final v1 catalog**

Run:

```bash
python3 tools/vault_migration/cli.py catalog \
  --contract-version 1 \
  --revision contract-v1 \
  --output tools/vault_migration/contracts/v1.json
```

Then verify:

```bash
python3 -m unittest builder_tests.test_vault_migration.ContractCatalogTest
```

Expected: catalog hashes match every managed file.

- [ ] **Step 4: Run the complete builder release gate**

Run:

```bash
scripts/release_gate.sh
```

Expected:

- all wiki and builder migration tests pass;
- lint and health report no issues;
- navigation maps are current;
- maintenance metrics pass;
- `git diff --check` passes;
- the gate does not change tracked output.

- [ ] **Step 5: Audit the real user Vault without writing**

Capture a before fingerprint, run audit without `--report`, then compare:

```bash
python3 - <<'PY'
from pathlib import Path
from tools.vault_migration.planning import target_fingerprint
from tools.vault_migration.managed_paths import MANAGED_PATHS

target = Path("/Users/junhyounglee/vault")
print(target_fingerprint(target, MANAGED_PATHS))
PY

python3 tools/vault_migration/cli.py audit \
  --target /Users/junhyounglee/vault

python3 - <<'PY'
from pathlib import Path
from tools.vault_migration.planning import target_fingerprint
from tools.vault_migration.managed_paths import MANAGED_PATHS

target = Path("/Users/junhyounglee/vault")
print(target_fingerprint(target, MANAGED_PATHS))
PY
```

Expected:

- audit exits 0 or 2;
- before and after fingerprints are identical;
- stdout lists create, replace, preserve, review-required, and rebuild-suggested counts;
- no migration plan, report, manifest, or recovery journal is written to the target.

Do not run `apply` against the real Vault in this task. Present the audit summary to the user and request separate approval.

- [ ] **Step 6: Commit final acceptance coverage**

```bash
git add builder_tests/test_vault_migration.py tools/vault_migration/contracts/v1.json
git commit -m "test: verify vault migration safety"
```

---

## Errors And Edge Cases

- Empty or non-Vault target: audit exits 1 with the missing topology path; no files are created.
- Manifest absent: compare target managed-file hashes against v0 and v1 catalogs.
- Manifest malformed or unknown version: emit `invalid`; do not fall back silently.
- Managed file missing: `create` only if the parent path is safe and no review action exists anywhere in the plan.
- Managed file customized: `review-required`; block the entire apply.
- User content exactly matches a managed-file hash by coincidence: path ownership wins, so preserve it.
- Raw source drift: report the existing source-drift issue; never modify raw or its source page.
- Duplicate canonical bodies: report both paths for review; never pick a winner.
- Generated view stale: emit `rebuild-suggested`; do not rebuild during apply.
- Audit output path inside target: reject before writing.
- Explicit report path outside `scratch/reports/`: reject before writing.
- Symlink target, parent, managed file, manifest, journal, plan, or report root: reject.
- Target changes after audit: fingerprint/precondition mismatch; require a fresh audit.
- Builder changes after audit: desired hash mismatch; require a fresh audit from one builder revision.
- Disk/write failure: roll back from the journal.
- Process interruption: preserve the journal and require `recover`.
- Release gate failure: restore managed files and do not advance the manifest.
- Rollback failure: preserve `rollback-failed` journal and print the exact recovery command.
- Optional Stenc Vault: preserve current Stenc files; do not catalog, create, replace, or delete them.

## Validation Review

- **Security/read-only:** Default audit has a tree-digest regression test; every target path uses `lstat`, root containment, and symlink rejection; apply uses an explicit plan and no force option.
- **Performance/scalability:** Audit hashes only the finite managed-file allowlist plus role-scoped Markdown needed for authority checks. It does not copy raw assets or load file bodies unrelated to role validation.
- **Testability:** Filesystem, gate runner, timestamps, and Git-baseline extraction have deterministic seams. Failure injection covers write, gate, and rollback paths.
- **Maintainability/deep modules:** `tools/vault_migration` hides catalog, plan, and recovery complexity behind `audit_vault()` and `apply_plan()`. Generated Vault tooling remains focused on wiki validation.
- **Agent ergonomics:** The normal workflow is two explicit commands, stable exit codes, human-readable action counts, and no need to understand internal file copying.

## Verification

Run from the `vault-builder` worktree:

```bash
python3 -m unittest tests.test_wiki_tools.WikiAuthorityContractTest
python3 -m unittest builder_tests.test_vault_migration
python3 -m unittest tests.test_wiki_quality_baseline tests.test_wiki_tools
scripts/release_gate.sh
git diff --check
git status --short
```

Expected final signals:

- all tests pass;
- the release gate exits 0;
- no tracked output is changed by verification;
- no target Vault content is changed by audit;
- only intentional implementation and documentation files remain in the worktree before the final commit.

## Risks

| Risk | Mitigation |
| --- | --- |
| A legacy user edit looks like an old builder file | Replace only hashes found in a versioned catalog; otherwise require review. |
| Catalog drift after implementation | Contract test compares v1 catalog hashes to the live managed allowlist on every builder gate. |
| Migration partially updates tools | Block on any review/invalid action, journal originals first, atomic replace, and roll back on every failure. |
| Authority validation breaks legacy Vaults | Normal legacy lint remains version-gated; migration audit surfaces violations before apply. |
| Recovery metadata becomes searchable knowledge | Store it below hidden `.vault-builder/`, remove it after success, and never copy it into wiki or reports. |
| Builder-only tests break generated Vault release gates | Keep tests in `builder_tests/` and run them conditionally only in the builder checkout. |
| Optional Stenc files gain accidental ownership | Exclude Stenc paths from v1 catalogs and assert existing opt-in initialization still passes. |

## Open Decisions

None. The user approved builder ownership, non-destructive legacy migration, explicit apply, role boundaries, and rollback behavior on 2026-07-30.
