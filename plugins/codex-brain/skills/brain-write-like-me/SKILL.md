---
name: brain-write-like-me
description: "Bootstrap a personal writing-style profile from the user's own sent messages (email, chat, or pasted samples), for use later when drafting text in their voice. Use when the user asks to learn their writing style, capture their voice, or create a write-like-me profile. Triggers: learn my writing style, write like me, capture my voice."
---

# Write-like-me bootstrap

This produces a durable style note in the vault, not a live connector
integration — the skill itself does not fetch anything on its own.

## Scope and consent

Ask explicitly which source to use before reading anything: pasted
samples the user provides directly, or messages from a connector already
available in the session (mail, chat) that the user names. Never assume a
connector is connected; check what the session actually exposes. Agree on
a bounded sample size (a handful of representative messages, not a full
mailbox) before reading.

Writing samples can reveal who the user corresponds with and what about —
treat the raw samples as sensitive input. Do not quote large verbatim
chunks into the resulting style note; extract patterns, not content.

## Extract style, not content

From the agreed sample, note things like: typical greeting/sign-off,
sentence length and rhythm, how directly the user disagrees or says no,
level of formality by audience, favorite phrases, what they never do
(e.g. exclamation points, emoji). Do not copy sentences verbatim into the
profile — describe the pattern.

Separate posture by audience if it clearly differs (e.g. terser with
colleagues, warmer with close collaborators) rather than flattening into
one voice.

## Write it as a transaction

Produce one note (for example `wiki/concepts/write-like-me.md` or a vault
convention the user prefers) describing the style, and nothing else.
Resolve the engine as described in `brain-init`, inspect the transaction,
show the user the draft before applying, and report the operation ID and
path once applied.

Treat the resulting note as a living document: update it in place with a
new reviewed transaction later, rather than creating a second profile.
