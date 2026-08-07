---
name: brain-new-person
description: "Create or update a person note under wiki/people/ from a template. Use when the user asks to add a collaborator, remember someone's preferences, or bootstrap a wiki/people/<name>.md file. Triggers: add a person, remember this about someone, create a people note."
---

# New person note

Create at most one note per person. Search `wiki/people/` for an existing note
with the same name (or an obvious alias) before creating a new one; update
the existing note instead of duplicating it.

Use `assets/person.md` in this skill's own folder as the starting
template. Fill in only what the user actually told you or what you read
from a source they explicitly approved for this purpose — never invent a
role, preference, or interaction to fill out the template.

## Sensitive content

Personal information about a real, identifiable individual deserves the
same care as any private data: confirm scope before capturing anything
beyond what is needed for the user's stated purpose, and skip fields the
user has not actually provided rather than guessing.

## Write it as a transaction

Build one transaction: the `wiki/people/<name>.md` note (create or replace),
plus a `wiki/index.md` update only if this vault indexes people there.
Never write the file directly with a generic edit tool — resolve the
engine as described in `brain-init`, run `transaction inspect`, show the
user the diff, then `transaction apply` with the approved hash.

Report the resulting operation ID and path once applied.
