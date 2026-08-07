---
name: brain-onboarding
description: "First-run setup for a codex-brain: initializes the vault if one does not exist yet, understands the workspace, asks what projects and people matter, checks for useful connected tools, and proactively proposes wiki/people/ and wiki/projects/ notes for approval. The single entry point for starting a new vault from scratch. Triggers: onboard me, set up my vault, first-time setup."
---

# Onboarding

Run this once when a vault is new or when the user explicitly asks to be
onboarded again. It gathers context and proposes drafts; it never writes
anything without the user reviewing and approving a transaction first.

## 0. Make sure a vault exists first

Check for `.codex-brain.json` (explicit `--vault`, `CODEX_BRAIN`, or the
nearest one above the current directory, same resolution order as
`brain-init`). If none is found, this is a brand-new directory: hand off
to `brain-init` first (new vault via `init`, or non-destructive `adopt` if
the directory already has content like `wiki/people/`/`wiki/projects/`). Only
proceed to step 1 once a vault is confirmed to exist — do not try to read
`wiki/hot.md` or ask onboarding questions against a directory that has no
vault yet.

## 1. Read before asking

Before asking the user anything, read what already exists: `wiki/hot.md`,
`wiki/index.md`, `wiki/people/*.md`, `wiki/projects/*.md`, and any `AGENTS.md` in the
vault. Do not ask a question this reading already answers.

## 2. Ask what matters

Ask, in plain language:

- what active projects should this vault track;
- who the user works with regularly (people worth having a note for);
- whether there is existing material to ingest (files in `inbox/`, or
  something the user wants pasted in now).

Keep this to a short focused exchange, not an exhaustive interview.

## 3. Check connected capabilities

List which read-capable tools are actually available in this session
(mail, calendar, drive, chat, issue tracker — whatever the host exposes)
and which of the "useful for onboarding" categories are missing. Report
this plainly; do not claim a capability is available when it is not
connected, and do not fetch anything from a connected tool without the
user's go-ahead for that specific scan.

## 4. Propose, do not apply

If the user agrees to a scan, read the agreed sources and draft candidate
notes: one `wiki/people/<name>.md` per person who comes up meaningfully, one
`wiki/projects/<name>.md` per active project. Show the full list of proposed
titles and a one-line reason for each before drafting content, so the user
can drop any of them before you spend the read/write budget.

For every accepted candidate, build one `brain-save`-style transaction
(read `wiki/hot.md`/`wiki/index.md` first, one bundle, one inspect, one
apply) — never write ad hoc. Never write `AGENTS.md` itself without a
separate, explicit approval, since it is a shared instruction file, not
vault content.

## 5. Offer recurring checks last

Only after the above, ask whether the user wants a recurring check (daily
or scheduled) that re-runs a bounded version of step 3/4 later. Be explicit
that a recurring check can only produce a fresh set of proposals for
review — it cannot apply a transaction unattended, because this plugin
never mutates the vault without a human approving the specific plan.

## Boundaries

Never send a message, change a calendar event, edit a document the user
did not ask you to edit, or install a new integration without asking
first. Never invent a person, project, or fact to fill out a note — an
honest "not enough context yet" beats a fabricated entry.
