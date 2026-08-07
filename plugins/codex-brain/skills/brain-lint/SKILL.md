---
name: brain-lint
description: "Run a deterministic health check on the vault: dead links, orphan pages, missing frontmatter, duplicate basenames, stale index entries, and provenance errors. Read-only — never writes. Use when the user asks to check vault health, find broken links, or clean up the wiki. Triggers: check the vault, lint the vault, find broken links, vault health check."
---

# Vault health check

Read-only. Never write, never propose a fix here — this skill only
reports; use `brain-save`/`brain-ingest`/`brain-new-person`/
`brain-new-project` separately for anything the report suggests fixing.

Resolve the engine and vault the same way as `brain-init`. The engine
lives in the `brain-init` skill's own `scripts/` folder; when both skills
are installed as siblings, resolve it relative to this skill's own
location:

```bash
SKILL_ROOT=/absolute/path/to/brain-lint
ENGINE="$SKILL_ROOT/../brain-init/scripts/vault.py"
python3 "$ENGINE" lint --vault /path/to/vault --format markdown
```

`--format json` gives a machine-readable report with the same fields;
`--format markdown` gives the same report as prose, which is usually
better for talking to the user. Add `--as-of YYYY-MM-DD` to check
provenance freshness as of a specific date instead of today. Add
`--strict` only when you specifically want a non-zero exit on any
finding (for example, wiring this into a script) — do not add it by
default, since a normal health-check conversation should still let you
read and summarize the report even when it finds issues.

## What it checks

Dead links, ambiguous link targets, duplicate basenames, orphan pages
(unlinked from the index), missing frontmatter, empty sections, stale
index entries, unreadable files, provenance/claim-ledger errors, and
allowlisted dangling links (already-known exceptions, reported separately
so they don't look like new problems).

## Reporting to the user

Summarize the counts first (pages scanned, links scanned, issues found),
then list only the categories that actually have findings — do not paste
the full report noise for empty categories. If `issues_found` is 0, say
so plainly instead of padding the answer.

If the user wants something fixed, treat that as a new, separately scoped
request: identify which skill applies (usually `brain-save` for content
issues) and follow its own transaction flow — this skill does not apply
fixes itself.
