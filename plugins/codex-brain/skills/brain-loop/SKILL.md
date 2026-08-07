---
name: brain-loop
description: "Set up a recurring, bounded check against this vault, orchestrated by an external scheduler (cron, CI) calling `codex exec`. Use when the user wants a periodic check-in, a recurring monitor, or a follow-up that repeats on a cadence. Triggers: check this again later, monitor this, remind me periodically, recurring check-in."
---

# Recurring vault check

Plain Codex CLI has no built-in in-thread heartbeat or scheduling
primitive — there is no native "run again in N hours" command. A real
recurring check needs an external scheduler (cron, a CI job, or any
system-level timer) calling `codex exec` with a fixed, bounded prompt. Say
this plainly rather than promising an in-thread loop that does not exist.

## Design the check before wiring anything

Agree with the user on:

- what to check (a bounded question against `wiki/`, `wiki/people/`, or
  `wiki/projects/` — not "everything");
- the cadence (daily, weekly — pick the loosest cadence that still serves
  the user, not the tightest);
- what a run should produce: a short written note the user reads later,
  never an unattended vault mutation.

## Wire it externally

Draft the exact non-interactive command the scheduler should run, for
example:

```bash
codex exec "Read wiki/hot.md and wiki/people/*.md in /path/to/vault. \
Report anything that looks stale or newly overdue, in under 10 lines. \
Do not write any file."
```

Show this command to the user and let them add it to their own
scheduler (cron, systemd timer, CI cron job) — this skill drafts the
command and the check's scope; it does not install a cron entry on the
user's behalf without a separate, explicit request to do so, since that
changes system state outside the vault.

## What has actually been verified

The orchestration mechanics were tested end-to-end with a real `cron`
entry: an externally-scheduled job invoked the engine's read-only
`hook session-start` against a real vault and produced a bounded report
without ever touching a write path — proving the "external scheduler
calls a bounded, read-only check" pattern genuinely works, not just in
theory. What is still unverified is `codex exec` itself, since no Codex
CLI was available in the environment this plugin was built in — draft
the exact command with the user and have them confirm it runs as
expected in their own environment before relying on it unattended.

## Hard boundary

A scheduled run may only read and report. If a scheduled check finds
something worth saving to the vault, it must say so in its output for the
user to act on in a live session — it must never call `brain-save`,
`brain-new-person`, or any other write skill unattended, because no
transaction in this plugin applies without a human reviewing the specific
plan first.
