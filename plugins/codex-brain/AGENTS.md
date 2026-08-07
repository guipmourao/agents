# codex-brain

Plugin manifest: `.codex-plugin/plugin.json`. Installation: see
`README.md`.

Persistent memory plugin for Codex: a local vault with writes always
reviewed through a transaction (plan -> approved hash -> apply), source/
claim provenance ledgers, and an onboarding skill that proposes content
instead of writing on its own.

## Core rule

No write to the user's vault happens outside a reviewed transaction: plan
(dry-run) -> approved hash -> apply. This applies to every skill in this
plugin. Never edit vault files directly with generic write tools.

## Engine

`skills/brain-init/scripts/vault.py` is the engine's entry point. Resolve
the user's vault via explicit `--vault`, `CODEX_BRAIN`, or the nearest
`.codex-brain.json` — never use this plugin's own root as a vault.

```bash
python3 skills/brain-init/scripts/vault.py --help
```

The other skills (`brain-save`, `brain-ingest`, `brain-query`,
`brain-onboarding`, `brain-new-person`, `brain-new-project`,
`brain-write-like-me`, `brain-assistant`, `brain-loop`) resolve the same
engine via a relative path, assuming every skill in this plugin is
installed as siblings (same parent directory under `~/.agents/skills/`).

## Hooks

`hooks/hooks.json` declares two lifecycle hooks, both read-only (neither
ever writes to the vault):

- `SessionStart` — injects `wiki/hot.md` into the session context.
  **Off by default**: only activates when the `CODEX_BRAIN_SESSION_CONTEXT=1`
  environment variable is set by the caller — without it, it emits an
  empty string. This is a deliberate privacy decision: reading the vault
  is local, but putting that content inside a hosted session is egress,
  and egress requires explicit user opt-in.
- `Stop` — warns (via `systemMessage`) when an incomplete transaction is
  left behind (a stuck `mutation.lock`, or a journal in `prepared`/
  `applying`/`rollback-failed` state) that needs `transaction recover`.

## Structure of a vault built with this plugin

- `wiki/` — generated knowledge (pages, index, log, hot cache, ledgers).
  Managed exclusively through transactions.
- `.raw/` — immutable source payloads.
- `wiki/people/` — people and collaborators.
- `wiki/projects/` — long-running active work.
- `wiki/experiments/` — short-lived, disposable investigations.
- `inbox/` — visible staging area for sources to process.

## Skills

- `skills/brain-init/` — initialize or adopt an existing vault.
- `skills/brain-save/` — persist a specific result from a conversation.
- `skills/brain-ingest/` — turn a supplied source into connected, cited
  pages.
- `skills/brain-query/` — answer using only what is already in the vault
  (read-only).
- `skills/brain-onboarding/` — first-run setup: understand the workspace,
  ask about relevant projects and people, and proactively propose notes
  in `wiki/people/` and `wiki/projects/` based on what it finds, always
  asking for approval before writing.
- `skills/brain-new-person/` — create or update a note in `wiki/people/`.
- `skills/brain-new-project/` — create a new project or experiment with a
  README (and optionally a local `AGENTS.md`).
- `skills/brain-write-like-me/` — extract a writing-style profile from
  samples the user approves.
- `skills/brain-assistant/` — ongoing support after onboarding: context,
  drafts, next steps.
- `skills/brain-loop/` — design a recurring check orchestrated by an
  external scheduler (`codex exec` via cron); never applies a write
  unattended.
