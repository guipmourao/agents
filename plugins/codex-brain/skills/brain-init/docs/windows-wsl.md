# Windows and WSL

`codex-brain` writes (`init`, `adopt`, `transaction apply`, `checkpoint`)
require WSL/Linux or supported macOS. Native Windows is refused before any
side effect, with the message that points here.

## The constraint is about the process, not just the vault path

This is the part that is easy to miss: **it does not matter where the
vault lives if the engine itself is running as a native Windows
process.** Pointing `--vault` at a WSL path from a Windows-native process
does not help — `--apply` still fails with `UNSUPPORTED_PLATFORM`,
because the confinement primitive (POSIX directory descriptors) has to
come from the process's own kernel, not from the target path.

Concretely: if you are using **Codex Desktop on Windows**, that app's own
process is native Windows. Any skill in this plugin that calls `--apply`
will be refused there, for any vault, on any path. Read-only work
(`brain-query`, dry-run plans, `brain-onboarding`'s planning step) is
fine from Codex Desktop — only the actual write needs a different
process.

The working setup is: run a Codex session **inside WSL** (open a WSL/Ubuntu
terminal, then run `codex` — the CLI, not the Desktop app — from there) for
`brain-init`'s `--apply`, `brain-save`, `brain-ingest`, and anything else
that writes. Desktop-on-Windows and CLI-in-WSL can both point at the same
vault; only the write side has to happen from inside WSL.

Confirmed in practice: Codex Desktop's own sandboxed process cannot
reliably reach WSL at all from inside a session, on either path — a
`\\wsl.localhost\...` UNC path and a `wsl.exe` subprocess call both
failed silently from inside a Codex Desktop session on a machine where
`wsl -l -v` worked fine from a plain PowerShell window on the same
machine. This is a sandboxing limitation of Codex Desktop itself, not a
broken WSL install — verify with the same plain-PowerShell test before
assuming WSL is the problem. If Codex Desktop genuinely cannot reach WSL
on your machine, the practical options are: (1) do the write-side work
from a Codex CLI session installed and run inside WSL directly (`npm
install -g @openai/codex`, then `codex login`, then `codex plugin
marketplace add ...` from a WSL terminal — not from Codex Desktop), or
(2) keep the vault on a native Windows path on that machine and accept
that Codex Desktop there is read-only for this plugin either way.

## Why writes require WSL

Mutation safety depends on POSIX directory descriptors: the vault root and
every runtime directory stay pinned for the whole write, so a concurrently
swapped symlink or replaced folder fails closed instead of silently
redirecting the write. Native Windows cannot provide that primitive, so
writes are refused up front instead of running with a weaker guarantee.

Read-only inspection and dry-run plans work fine on native Windows — only
`--apply` and other mutating commands are blocked.

## Keep the vault off `/mnt/c`

A vault that lives on a Windows drive mounted into WSL (`/mnt/c/...`, aka
DrvFs) can hit a specific failure: DrvFs does not preserve Unix permission
bits — every file reports back as `0777` regardless of what the engine
wrote. The engine writes a file with a specific mode, then re-reads it to
verify, and that verification fails with a mode mismatch even though the
content is correct. The transaction rolls itself back cleanly when this
happens (nothing is left half-written), but the operation cannot succeed
on that path.

The fix is not a flag — move the vault into the WSL filesystem itself
(for example `/home/<user>/<vault>`), not a mounted Windows drive. Access
it from Windows tools by path (`\\wsl.localhost\<distro>\home\<user>\<vault>`)
when you need to, but keep the vault's actual home in WSL.

A vault on FAT/exFAT (typical USB sticks) or some network shares fails
differently, with `UNSAFE_VAULT_IDENTITY` — those filesystems don't offer
stable-enough file identity either. Same fix: move it to NTFS or into WSL.

## WSL misbehaving: troubleshooting

WSL being "installed" does not always mean it is working. Roughly in the
order worth trying:

| Symptom | Check |
|---|---|
| `wsl --install` finished but `wsl --status` / `wsl -l -v` hangs | Usually a virtualization conflict, not a `codex-brain` issue — see the checklist below. |
| `wsl` reports a kernel or version error | Run `wsl --update`, then `wsl --shutdown`, then retry. |
| WSL worked before and stopped after a Windows update or new security software | Check whether Virtualization-Based Security / memory integrity changed — VBS and other hypervisors can conflict with the Hyper-V platform WSL2 depends on. |
| A dry-run's approval hash produced on native Windows fails inside WSL with a plan-changed error | Expected: the approval hash binds to the filesystem identity of the environment that produced it. Run the dry-run review inside WSL when the apply will also happen there. |
| Writes fail with `UNSAFE_VAULT_IDENTITY` | See "Keep the vault off `/mnt/c`" above. |
| `wsl -l -v` (or similar) returns `E_ACCESSDENIED` when run from inside a sandboxed app (Codex Desktop, an IDE's integrated terminal, etc.) | Test the exact same command from a plain PowerShell/cmd window you opened yourself first. If it works there but not from inside the app, the app's own sandboxed process lacks the token/privilege to invoke `wsl.exe` — that is a limitation of that app's execution environment, not a broken WSL install. Do the write-side work from a real WSL terminal instead of expecting the sandboxed app to reach WSL for you. |

Virtualization checklist for the hang class of problem:

1. Confirm virtualization is enabled in BIOS/UEFI ("Intel VT-x", "AMD-V",
   or "SVM").
2. Confirm the Windows features "Virtual Machine Platform" and "Windows
   Subsystem for Linux" are both enabled, and reboot — the reboot is not
   optional.
3. Run `wsl --update` from an elevated prompt, then `wsl --shutdown`,
   then retry `wsl --status`.
4. Check that the Hyper-V "Host Compute Service" (`vmcompute`) is
   running; restart it if stopped.
5. If it still hangs, look for a conflict between Virtualization-Based
   Security and a third-party hypervisor — this is hardware- and
   configuration-specific and can survive reboots until the conflicting
   feature is reconfigured.

## GUI apps from WSL (Obsidian, editors, etc.)

WSLg (bundled with modern WSL2) can run Linux GUI apps and show them as
normal windows on the Windows desktop — useful if a vault is meant to be
browsed with a native Linux app instead of a Windows one. Two practical
notes from running this in practice:

- An AppImage may fail with a FUSE error (`dlopen(): error loading
  libfuse.so.2`) on distros that no longer ship `libfuse2` by default.
  Extract it once (`./App.AppImage --appimage-extract`) and run the
  extracted `AppRun` directly instead of installing a legacy FUSE
  compatibility package.
- A Windows app pointed at a WSL path over `\\wsl.localhost\...` can fail
  to *watch* that path for changes (`EISDIR: illegal operation on a
  directory, watch ...`), even though plain file reads/writes over that
  same path work fine. That is a 9P-protocol limitation on directory
  watching, not a `codex-brain` issue — if an app needs to watch the vault
  directory live, run that app inside WSL against the native path instead
  of reaching in from Windows.
