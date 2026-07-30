#!/usr/bin/env python3
"""Builder-only Vault migration utility."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.vault_migration.catalogs import build_catalog, write_catalog
from tools.vault_migration.manifests import create_manifest, write_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vault-migration")
    subcommands = parser.add_subparsers(dest="command", required=True)
    catalog = subcommands.add_parser("catalog")
    catalog.add_argument("--contract-version", required=True, type=int)
    catalog.add_argument("--revision", required=True)
    catalog.add_argument("--source-ref")
    catalog.add_argument("--output", required=True)
    manifest = subcommands.add_parser("manifest")
    manifest.add_argument("--builder-root", required=True)
    manifest.add_argument("--target", required=True)
    manifest.add_argument("--builder-revision", required=True)

    args = parser.parse_args(argv)
    if args.command == "catalog":
        try:
            result = build_catalog(ROOT, args.contract_version, args.revision, args.source_ref)
            output = Path(args.output)
            display_output = output if output.is_absolute() else ROOT / output
            write_catalog(output, result, output_root=ROOT)
        except (OSError, ValueError) as exc:
            print(f"catalog: {exc}", file=sys.stderr)
            return 1
        print(display_output)
        return 0
    if args.command == "manifest":
        try:
            result = create_manifest(
                Path(args.builder_root),
                Path(args.target),
                args.builder_revision,
            )
            output = write_manifest(Path(args.target), result)
        except (OSError, ValueError) as exc:
            print(f"manifest: {exc}", file=sys.stderr)
            return 1
        print(f"{output} (contract v{result.contract_version})")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
