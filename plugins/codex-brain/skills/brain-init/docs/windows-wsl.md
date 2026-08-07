# Windows: native and WSL

`codex-brain` writes (`init`, `adopt`, `transaction apply`, `checkpoint`)
now work two ways on Windows:

- **Native Windows** (NTFS or ReFS, local volume): works directly, behind a
  one-time `pip install pywin32` — see "Native Windows setup" below.
- **WSL/Linux or supported macOS**: works as it always has, with the
  strongest safety guarantee (see "Guarantee tiers" below).

FAT/exFAT and network shares are refused on both paths — see "Guarantee
tiers".

## Guarantee tiers

Every write resolves one of three tiers for the vault's volume:

- **STRICT** (WSL/Linux/macOS): the vault root and every runtime directory
  stay pinned by a kernel-enforced directory descriptor for the whole write,
  so a concurrently swapped symlink or replaced folder fails closed instead
  of silently redirecting the write.
- **COMPATIBLE** (native Windows, NTFS/ReFS local volume): Windows has no
  equivalent to that descriptor primitive, so each path component is opened
  by full path and its identity verified immediately afterward instead of
  pinned in advance. This narrows the window where a concurrent replace
  could redirect a write, rather than eliminating it the way STRICT does.
  Process-exclusivity locking uses a dedicated lock file inside the vault's
  metadata directory (`LockFileEx` doesn't accept directory handles on
  Windows), not the vault root directory itself.
- **UNSAFE_REFUSED** (FAT/exFAT, unclassified network shares, or an
  unrecognized volume): writes are refused outright, on either platform —
  these filesystems don't expose stable-enough file identity for either
  tier's safety checks.

A completed transaction's journal records which tier produced it.

## Native Windows setup

1. One-time: `pip install pywin32`. The engine imports it lazily and only
   for mutating operations — nothing else in this codebase depends on it, so
   read-only inspection and dry-runs never need it.
2. Run the same commands you would inside WSL — `init`, `adopt`,
   `transaction apply`, `checkpoint` — directly from a native Windows Python.
   Codex Desktop on Windows can now do the write step itself in this mode;
   you no longer need a separate WSL/CLI session just to mutate the vault.

WSL continues to work exactly as documented below, with no setup change, if
you'd rather use it.

## OneDrive and Controlled Folder Access

These are the two things most native-Windows users hit first, since a
default Windows 11 install puts Documents/Desktop under both.

**OneDrive Files On-Demand.** A file that hasn't been downloaded locally yet
is a placeholder on disk. Every read/write in this engine opens files the
ordinary way (no special "don't hydrate" flag), and that is documented
OneDrive behavior to trigger a real download on its own — so this generally
just works, with a possibly slower first touch of a cold file. If a
first-touch read or write times out or fails outright, retry once OneDrive
has finished syncing that file.

**Controlled Folder Access** (Windows Defender Exploit Guard) can block
writes to Documents/Desktop/Pictures/Videos/Music/Favorites from processes
it doesn't recognize. When this happens, the engine detects it (Windows
gives no distinguishing error code of its own, so this is a best-effort
check based on where the write is happening) and raises a
`CONTROLLED_FOLDER_ACCESS_BLOCKED` error instead of a generic permission
error. Fix it by either:
- Adding the process (or the vault folder) to the allow-list in
  **Windows Security → App & browser control → Exploit protection →
  Controlled folder access**, or
- Moving the vault out of a CFA-protected default folder.

## WSL setup (STRICT tier)

The rest of this document covers running the write side inside WSL, which
gives the STRICT tier and needs no `pywin32` setup. This remains a fully
supported path, not a fallback.

### The constraint is about the process, not just the vault path

This is the part that is easy to miss: **the tier is determined by which
process performs the write, not by where the vault path points.** Pointing
`--vault` at a WSL path from a Windows-native process does not upgrade you
to STRICT — the write runs in whatever tier that process's platform
supports (COMPATIBLE on native Windows, STRICT inside WSL/Linux/macOS).

Concretely: a **Codex Desktop on Windows** session on a FAT/exFAT or
unclassified-network vault still gets refused for any `--apply`-calling
skill, same as before native Windows support existed — only that specific
refusal case remains. Read-only work (`brain-query`, dry-run plans,
`brain-onboarding`'s planning step) is fine from Codex Desktop either way.

The WSL-based working setup is: run a Codex session **inside WSL** (open a
WSL/Ubuntu terminal, then run `codex` — the CLI, not the Desktop app — from
there) for `brain-init`'s `--apply`, `brain-save`, `brain-ingest`, and
anything else that writes. Desktop-on-Windows and CLI-in-WSL can both point
at the same vault; only the write side has to happen from inside WSL (or,
now, from a native Windows process directly).

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
(2) use native Windows support instead (see above) — `pip install pywin32`
once, then Codex Desktop itself can do the write.

### Why STRICT writes require WSL/Linux/macOS

Mutation safety at the STRICT tier depends on POSIX directory descriptors:
the vault root and every runtime directory stay pinned for the whole write,
so a concurrently swapped symlink or replaced folder fails closed instead
of silently redirecting the write. Native Windows cannot provide that
primitive at all, which is why the COMPATIBLE tier above exists as a
distinct, narrower guarantee instead of trying to fake STRICT there.

Read-only inspection and dry-run plans work fine on native Windows in
either case — only `--apply` and other mutating commands care about the
tier.

### Keep the vault off `/mnt/c`

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
stable-enough file identity either, in WSL or natively. Same fix: move it
to NTFS/ReFS or into the WSL filesystem.

### WSL misbehaving: troubleshooting

WSL being "installed" does not always mean it is working. Roughly in the
order worth trying:

| Symptom | Check |
|---|---|
| `wsl --install` finished but `wsl --status` / `wsl -l -v` hangs | Usually a virtualization conflict, not a `codex-brain` issue — see the checklist below. |
| `wsl` reports a kernel or version error | Run `wsl --update`, then `wsl --shutdown`, then retry. |
| WSL worked before and stopped after a Windows update or new security software | Check whether Virtualization-Based Security / memory integrity changed — VBS and other hypervisors can conflict with the Hyper-V platform WSL2 depends on. |
| A dry-run's approval hash produced in one environment fails to apply in another with a plan-changed error | Expected: the approval hash binds to the filesystem identity of the environment that produced it. Native Windows, WSL, and macOS are three distinct environments for this purpose — run the dry-run review in the same environment where the apply will happen (native-Windows-to-native-Windows, WSL-to-WSL, or macOS-to-macOS), not mixed. |
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

### GUI apps from WSL (Obsidian, editors, etc.)

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
  of reaching in from Windows, or use the native-Windows write path above
  and run Obsidian directly against the native path with no WSL involved
  at all.
