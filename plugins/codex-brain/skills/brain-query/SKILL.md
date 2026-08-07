---
name: brain-query
description: "Answer a question using only existing vault knowledge. Read-only — never writes. Use for recall questions about the vault's own content. Triggers: what do we know about, search the vault, what did we decide about."
---

# Query the vault

Read-only. Never write, never update the hot cache, and never persist
anything merely because a question was asked — persistence from a query is
always a separate, explicitly requested `brain-save` operation.

Resolve the vault the same way as `brain-init`. Read `wiki/hot.md`,
`wiki/index.md`, and the pages relevant to the question. Check
`wiki/people/` and `wiki/projects/` too when the question is about a
person or an active project.

Answer from what the ledgers and pages actually say. If a claim in the
vault is marked `provisional`, `contested`, or `unsupported`, say so instead
of presenting it as settled fact. If the vault has no answer, say that
plainly rather than inventing one.
