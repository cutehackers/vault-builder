---
name: session-capture
description: Archive a Claude Code session transcript into the linked project vault's raw/sessions/ by running sessions capture --transcript, then report what was written. Never edit session files.
---

# Session Capture

Use this skill when the user asks to archive, save, or keep a Claude Code session in the vault.

The user-facing request usually looks like:

```text
이 세션 보관해줘.
```

## Boundary

```text
Skill = cognition
CLI = mutation and validation
Wiki/Git = durable state
```

`raw/sessions/` is the session archive: a separate raw source kind with no source pages. Only `python3 tools/wiki/cli.py sessions capture` adds files there, and it only adds new files. The full contract is `docs/agent/OPERATING-SCHEMA.md` section 16.1.

Never edit, rename, move, or delete a file under `raw/sessions/`, even to fix a lint error. Never write a session file by hand or with any other tool. Never run `ingest-source` or `bulk-ingest` on a session file, and never create a source page for one.

## Workflow

1. Find the transcript. Claude Code keeps it at `<config dir>/projects/<key>/<session id>.jsonl`. `<config dir>` is `CLAUDE_CONFIG_DIR`, else `$HOME/.claude`. `<key>` is the repository root absolute path with every non-alphanumeric character replaced by `-`. For the current session, use the session id if you know it, else the most recently modified `.jsonl` in that folder. If the user named another session or the choice is unclear, ask the user which file to use.
2. Run the capture command in manual form from this vault:

   ```bash
   python3 tools/wiki/cli.py sessions capture --transcript <transcript path> --cwd <repository root>
   ```

   - Add `--session-id <id>` only when the file name stem is not the session id.
   - Add `--vault <path>` only when the command reports more than one `project` vault as candidates and the user chose one.
   - Do not pass `--trigger`. Manual runs use `manual`.
3. Read stdout and stderr. The command chooses the target vault itself: the single role `project` vault linked to the session's project. It never writes to `personal` or `team` vaults.
4. When the command writes nothing, report its reason line and stop. Do not work around it:
   - No `project` vault linked: point the user to `docs/usage.md` section 17.7 ("project vault 연결하기"). Creating the vault, changing its role, and running `link` are the user's steps.
   - Target vault manifest missing or below contract 3: tell the user to run migration audit and apply themselves (`docs/usage.md` section 1.1). Never run migration `apply` and never edit `.vault-builder/manifest.json`.
   - `no new turns`: the session is already archived up to its last turn.
5. When a file was written, run `python3 tools/wiki/cli.py sessions status` in the target vault and report the uncommitted session file count. The capture command does not commit. Commit the new session file only when the user asks, and only after `scripts/release_gate.sh` passes in that vault.
6. Report: the target vault, the new file path, the result of `sessions status`, and any stderr notes (for example, skipped transcript lines or a previous part with a body hash mismatch).

## Lint Errors on Session Files

If `lint` reports a `session-` code or `claim-evidence-session-invalid`, do not change the session file. Report the code and path to the user. A committed file can be restored from Git history by the user. Put judgment-heavy cases under `scratch/review/`.

## Quality Gates

- Only `sessions capture` wrote under `raw/sessions/`, and only new files.
- No existing session file changed bytes, name, or location.
- No source page points into `raw/sessions/`.
- No migration `apply` ran and `.vault-builder/manifest.json` was not touched.
