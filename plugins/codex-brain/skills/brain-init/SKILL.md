---
name: brain-init
description: "Initialize a new vault or adopt an existing directory as a codex-brain. Use for first-time setup, adding memory to an existing repo, or choosing a vault when multiple exist. Triggers: set up vault, adopt this repo, init memory, first-time setup."
---

# Vault init / adopt

Treat this skill's own directory as code, never as a vault. Resolve the
engine by absolute path from this skill's own location:

```bash
SKILL_ROOT=/absolute/path/to/brain-init
ENGINE="$SKILL_ROOT/scripts/vault.py"
python3 "$ENGINE" --help
```

Resolve the target vault in this order: explicit `--vault`,
`CODEX_BRAIN`, the nearest `.codex-brain.json`, then an unambiguous vault
at or above the current directory. Fail closed on an ambiguous or missing
selection.

Writes (`init --apply`, `adopt --apply`, and everything downstream) work on
WSL/Linux/macOS as always, and now also work directly on native Windows
(NTFS/ReFS) behind a one-time `pip install pywin32` — see
`docs/windows-wsl.md` for setup, the OneDrive/Controlled Folder Access
notes, and what changes when running from Codex Desktop on Windows.

## New vault

```bash
python3 "$ENGINE" init /absolute/path/to/vault \
  --generated-at <ISO-UTC> --operation-id init-reviewed
python3 "$ENGINE" init /absolute/path/to/vault \
  --generated-at <ISO-UTC> --operation-id init-reviewed \
  --approved-plan-sha256 <hash-from-dry-run> --apply
```

## Existing directory

`adopt` is non-destructive: it only adds the missing `wiki/`, `.raw/`, and
ledger scaffolding. It never touches any existing content outside `wiki/`.

```bash
python3 "$ENGINE" adopt /absolute/path/to/vault \
  --generated-at <ISO-UTC> --operation-id adopt-reviewed
python3 "$ENGINE" adopt /absolute/path/to/vault \
  --generated-at <ISO-UTC> --operation-id adopt-reviewed \
  --approved-plan-sha256 <hash-from-dry-run> --apply
```

Always run the dry-run first, show the user the exact changed-path preview,
and only then repeat the identical command with `--apply` and the emitted
`approved_plan_sha256`. Never use `--force` unless the user has reviewed a
conflict and explicitly approved replacement.

Person and project notes for this plugin live under `wiki/people/` and
`wiki/projects/` (the transaction engine only accepts writes inside
`wiki/`). If the adopted directory already has its own root-level
`people/` or `projects/` folders from another tool, leave them where they
are — do not move their content into `wiki/` without a separate,
explicitly approved migration.

## Next steps

Point the user to `brain-onboarding` for first-run context gathering, or to
`brain-ingest` / `brain-save` for day-to-day use.
