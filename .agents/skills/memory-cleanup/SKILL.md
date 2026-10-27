---
name: memory-cleanup
description: Move unprocessed Claude Code L1 memory notes of linked projects into the right vault as raw copies, source pages, and validated wiki pages, leaving the L1 files untouched.
---

# Memory Cleanup

Use this skill when the user asks, in a vault session, to clean up, organize, or import Claude Code memory (L1 memory under `<config dir>/projects/<key>/memory/`).

The user-facing request usually looks like:

```text
메모리 정리해주세요.
```

## Boundary

```text
Skill = cognition
CLI = mutation and validation
Wiki/Git = durable state
```

Never edit, move, or delete L1 memory files. Read them only. The vault map `<config dir>/vault-builder/vault-map.json` (`<config dir>` is `CLAUDE_CONFIG_DIR`, else `$HOME/.claude`) lists vaults by name with `path` and `role`, and projects by project id with `root` and `vaults`. Do not keep a separate state file; the source pages' `raw_sha256` values are the record of what was imported.

## Workflow

1. Run `python3 tools/wiki/cli.py memory-scan --json` (add `--project <id>` for one project). It is read-only. It lists each linked project's notes with `sha256`, note `type`, `raw_name`, and `imported`. Exit 1 means the vault map is missing or does not list this vault; report the line and stop.
2. Skip `MEMORY.md`; it is only the list of notes, and `memory-scan` already leaves it out. Read each remaining note. The note type is either top-level `type` or `type` under `metadata:`.
3. Skip every note with `imported: true`. Another vault linked to the project may already hold it.
4. Choose one target vault per note by role:
   - project decisions and history go to the `project` vault,
   - knowledge the team agreed on goes to the `team` vault, through a PR only (step 9),
   - knowledge useful outside this project goes to the `personal` vault,
   - if no vault with that role is linked, use `personal` and say so in the report.
5. Copy the note bytes unchanged to `raw/imports/claude-memory/<project id>--<note stem>--<sha256 first 8>.md` in the target vault. `raw_name` from step 1 is this name. A changed note has a new hash, so it gets a new raw file; leave the older raw file and its source page as they are.
6. In the target vault, run `python3 tools/wiki/cli.py ingest-source <raw> --summary "<where the note came from>" --report`.
7. Find existing pages first with `python3 tools/wiki/cli.py search "<keywords>"`, then write drafts under `scratch/drafts/` and publish them with `publish-draft --report`:
   - Use `quality.provenance: claim` with a Claim/Status/Evidence table. Evidence is the raw copy, `raw/imports/claude-memory/<raw name>#sha256=<hash>`.
   - Add `project: <project id>` to project knowledge.
   - Section classification: a decision is `type: decision`. A user correction or working rule, including every L1 note of type `feedback`, gets the tag `correction` or `rule`.
   - If the note changes a decision or rule that already has a page, write a `patch-page` draft with `supersede-claim` (see `docs/agent/DRAFTS.md` and `docs/agent/OPERATING-SCHEMA.md` 2.3.1). Do not rewrite the page and do not open a contradiction review for a newer statement.
8. Contradictions: when a note conflicts with independent sources, or it is unclear which statement is newer, do not publish that note. Publish all other notes, pass the release gate, and commit first. Then run `python3 tools/wiki/cli.py review create --type contradiction --summary "..." --evidence <raw>` for each conflict, show the list to the user, and ask for a decision in the same session. Apply decisions with `review resolve`. If the user defers, the review stays pending; `metrics --check` only warns until the oldest unresolved review is older than 14 days, then it fails. Report the remaining reviews with their creation dates and the 14-day limit.
9. Team vault: work on a new branch `memory-cleanup/<YYYY-MM-DD>` and open a PR with `gh pr create`. Never commit to the default branch. If `gh` is missing, stop before push and report.
10. Finish: in every changed vault run `python3 tools/wiki/cli.py maps build --report` and `scripts/release_gate.sh`. Commit personal and project vaults only after the gate passes.
11. Report per vault: notes imported, pages created or patched, notes skipped as imported, notes sent to `personal` for lack of a role, open contradiction reviews, and PR links.

## Quality Gates

- L1 files keep their bytes and mtime.
- Do not mutate files under `raw/` after copying them.
- Running the skill again on unchanged notes creates no files.
- Do not publish a note that conflicts with existing knowledge before the user decides.
- Run `scripts/release_gate.sh` before reporting that durable wiki state is ready.
