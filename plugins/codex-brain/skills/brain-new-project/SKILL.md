---
name: brain-new-project
description: "Bootstrap a new project or experiment directory with a README and optional PROJECT_AGENTS.md. Use when the user asks to start a project, start an experiment/spike, or scaffold a workspace entry. Triggers: new project, start an experiment, scaffold a project folder."
---

# New project / experiment

Ask one thing first: is this a `wiki/projects/` entry (long-lived, ongoing) or
an `wiki/experiments/` entry (short-lived spike, expected to end in days)? Use
the corresponding template in this skill's `assets/` folder:

- `assets/project_README.md` for `wiki/projects/<name>/README.md`
- `assets/experiment_README.md` for `wiki/experiments/<name>/README.md`
- `assets/PROJECT_AGENTS.md` for an optional `wiki/projects/<name>/AGENTS.md`,
  only when the user says this project needs its own local rules

Fill in the goal/question from what the user actually said. Do not invent
a status, a stakeholder, or a finding.

Check for an existing folder with the same or a very similar name before
creating one; propose updating it instead of creating a near-duplicate.

## Write it as a transaction

Resolve the engine as described in `brain-init`. Build one transaction for
the new folder's file(s), inspect it, show the user the plan, then apply
with the approved hash. Report the operation ID and paths once applied.
