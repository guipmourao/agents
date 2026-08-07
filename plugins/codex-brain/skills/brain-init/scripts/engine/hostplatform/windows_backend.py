"""Native Windows primitive wrappers.

Every ``pywin32`` import here is local to the function that needs it, so
importing this module never fails on a host without ``pywin32`` installed —
only calling one of these functions does, with a clear
:class:`MissingDependencyError` instead of a raw ``ImportError`` traceback.

Reached only on native Windows, and only for mutating operations. Read-only
inspection stays on plain ``os``/``pathlib`` calls, which already work fine
on Windows without any of this.

Design note: ``os.open`` cannot open a directory at all on Windows (there is
no C-runtime directory descriptor), which is a stronger gap than "no dir_fd
support" — it is why a Windows backend needs real Win32 calls even for the
non-relative parts of the job. Everything that *does* have a working
full-path stdlib equivalent on Windows (``os.replace`` for atomic rename,
``os.link`` for hardlinks, ``os.lstat``/``os.stat`` for identity and
reparse-tag inspection, both already exercised by ``paths.py``) is
deliberately NOT re-wrapped here; only directory-handle acquisition,
directory locking, and the two Windows-only diagnostics
(fd→path recovery, Controlled Folder Access detection) need one.
"""

from __future__ import annotations

import os
import stat as stat_module
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


class MissingDependencyError(RuntimeError):
    """pywin32 is required for native Windows vault writes and isn't installed."""


class ControlledFolderAccessBlocked(OSError):
    """Raised in place of a bare PermissionError when Windows Defender's
    Controlled Folder Access is the likely cause of an ACCESS_DENIED write
    failure, so callers get an actionable message instead of a generic one."""


def _win32file():
    try:
        import win32file
    except ImportError as exc:  # pragma: no cover - only reachable on Windows
        raise MissingDependencyError(
            "native Windows vault writes require pywin32; install it with "
            "'pip install pywin32'"
        ) from exc
    return win32file


@dataclass(frozen=True)
class WindowsIdentity:
    """The Windows analogue of a POSIX ``(st_dev, st_ino)`` pair, built from
    ``GetFileInformationByHandle`` rather than ``os.stat`` so it reflects the
    exact handle that was opened and locked, not a fresh lookup that could
    race against a concurrent replace."""

    volume_serial_number: int
    file_index_high: int
    file_index_low: int

    def is_stable(self) -> bool:
        # A zero file index, like a zero POSIX inode, signals a filesystem
        # that doesn't expose stable identity (observed on some FAT/exFAT and
        # SMB redirectors) -- equality can never be asserted in that case.
        return not (self.file_index_high == 0 and self.file_index_low == 0)

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.volume_serial_number, self.file_index_high, self.file_index_low)


@dataclass
class DirectoryHandle:
    """An open, lockable directory handle plus the identity it was opened at."""

    path: Path
    handle: object
    identity: WindowsIdentity


def open_directory(
    path: Path,
    *,
    _after_open_hook: Optional[Callable[[Path], None]] = None,
) -> DirectoryHandle:
    """Open ``path`` as a directory handle and capture its identity.

    This is a single, full-path open — not a dir_fd-relative walk — so it
    gives the COMPATIBLE tier (open, then verify) rather than STRICT
    confinement. Intermediate path-component checks along a walk should use
    ``paths.assert_unaliased_directory`` (plain ``os.lstat``, already
    cross-platform) before reaching the final directory opened here.

    ``_after_open_hook`` is test-only and never runs in production: it lets
    tests deterministically inject a filesystem mutation between the open
    and the identity read, so the narrow TOCTOU window this design accepts
    is actually exercised rather than only the happy path.
    """

    win32file = _win32file()
    import win32con

    handle = win32file.CreateFile(
        str(path),
        win32con.GENERIC_READ,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
        None,
        win32con.OPEN_EXISTING,
        win32con.FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    try:
        if _after_open_hook is not None:
            _after_open_hook(path)
        info = win32file.GetFileInformationByHandle(handle)
    except Exception:
        handle.Close()
        raise
    # info is expected to be a 10-tuple matching BY_HANDLE_FILE_INFORMATION's
    # field order (dwFileAttributes, ftCreationTime, ftLastAccessTime,
    # ftLastWriteTime, dwVolumeSerialNumber, nFileSizeHigh, nFileSizeLow,
    # nNumberOfLinks, nFileIndexHigh, nFileIndexLow) -- this is the single
    # highest-risk unverified assumption in the whole Windows port: if
    # pywin32's actual tuple order differs, every identity comparison on
    # Windows is silently wrong. Confirm this against a real
    # GetFileInformationByHandle call the first time the Windows CI job
    # (docs/windows-wsl.md) runs, before trusting it further.
    identity = WindowsIdentity(
        volume_serial_number=info[4],
        file_index_high=info[8],
        file_index_low=info[9],
    )
    return DirectoryHandle(path=path, handle=handle, identity=identity)


def close_directory(handle: DirectoryHandle) -> None:
    handle.handle.Close()


def try_acquire_exclusive(handle: DirectoryHandle) -> bool:
    """Non-blocking exclusive lock, mirroring ``fcntl.flock(LOCK_EX|LOCK_NB)``.

    Windows supports locking a directory handle directly — unlike POSIX's
    directory-``flock`` portability quirks, this is not a degraded
    substitute, it is the equivalent-strength primitive for this one piece.
    """

    win32file = _win32file()
    import pywintypes
    import win32con

    overlapped = pywintypes.OVERLAPPED()
    try:
        # pywin32's LockFileEx takes a single combined byte-count arg, not
        # separate low/high DWORDs like the raw Win32 API -- confirmed by the
        # third real Windows CI run, which hit
        # "TypeError: LockFileEx() takes exactly 5 arguments (6 given)"
        # against the 6-arg (low, high split) call this used to make.
        win32file.LockFileEx(
            handle.handle,
            win32con.LOCKFILE_EXCLUSIVE_LOCK | win32con.LOCKFILE_FAIL_IMMEDIATELY,
            0,
            0xFFFFFFFFFFFFFFFF,
            overlapped,
        )
    except pywintypes.error as exc:
        # The expected "someone else holds this lock" outcome is
        # winerror==33 (ERROR_LOCK_VIOLATION). Anything else caught here is
        # still a pywintypes.error (so it can't be told apart from real
        # contention by type alone), but printing it means a genuine bug --
        # e.g. locking not being supported at all on a directory handle --
        # shows up in CI logs instead of just timing out silently, the same
        # blind-timeout failure mode the fourth real Windows CI run hit.
        import sys

        print(
            f"codex-brain: LockFileEx failed on {handle.path!r}: "
            f"winerror={getattr(exc, 'winerror', None)!r} {exc!r}",
            file=sys.stderr,
        )
        return False
    return True


def release_exclusive(handle: DirectoryHandle) -> None:
    win32file = _win32file()
    import pywintypes

    # Same combined-byte-count signature as LockFileEx above (4 args, not 5).
    win32file.UnlockFileEx(handle.handle, 0, 0xFFFFFFFFFFFFFFFF, pywintypes.OVERLAPPED())


def final_path_for_handle(handle: DirectoryHandle) -> Path:
    """``fcntl.F_GETPATH`` equivalent: recover the current path of an open
    handle. Used by checkpoint.py's fd→path recovery, alongside the existing
    linux/darwin branches there."""

    win32file = _win32file()
    raw = win32file.GetFinalPathNameByHandle(handle.handle, 0)
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    return Path(raw)


# Folders Controlled Folder Access protects by default. Resolved through
# SHGetKnownFolderPath at call time (see is_likely_cfa_block), not compared
# by these hardcoded, locale-specific display names.
_CFA_PROTECTED_KNOWN_FOLDERS: tuple[str, ...] = (
    "FOLDERID_Documents",
    "FOLDERID_Desktop",
    "FOLDERID_Pictures",
    "FOLDERID_Videos",
    "FOLDERID_Music",
    "FOLDERID_Favorites",
)


def is_likely_cfa_block(path: Path, exc: OSError) -> bool:
    """True when an ACCESS_DENIED write failure is plausibly Windows
    Defender's Controlled Folder Access rather than an ordinary permission
    problem.

    Best-effort: CFA raises no error code of its own to distinguish itself,
    so this checks whether ``path`` sits under a folder CFA protects by
    default. Callers should re-raise as :class:`ControlledFolderAccessBlocked`
    when this returns True, instead of surfacing a generic ``PermissionError``.

    NOTE: the exact ``pywin32``/``shellcon`` constant names used here have
    not been exercised against a real pywin32 install (none is available in
    this development environment) — treat as unverified until the Windows CI
    job (see docs/windows-wsl.md) runs it for real, per the plan's rollout
    rigor.
    """

    import winerror

    if getattr(exc, "winerror", None) != winerror.ERROR_ACCESS_DENIED:
        return False
    try:
        import win32com.shell.shell as shell
        import win32com.shell.shellcon as shellcon
    except ImportError:
        return False
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for constant_name in _CFA_PROTECTED_KNOWN_FOLDERS:
        constant = getattr(shellcon, constant_name, None)
        if constant is None:
            continue
        try:
            known = Path(shell.SHGetKnownFolderPath(constant, 0))
            resolved.relative_to(known)
        except Exception:  # noqa: BLE001 - best-effort probe, any failure means "no match"
            continue
        return True
    return False


# --- Directory-record primitives (COMPATIBLE tier) -------------------------
#
# The functions below are the Windows counterpart to posix_backend.py's
# dir_fd-relative record primitives, dispatched to from hostplatform.dirops.
# Unlike directory acquisition/locking above, most of these need no pywin32
# call at all: os.open/os.stat/os.link/os.unlink/os.scandir all work fine on
# Windows for *regular files* opened by full path (only directories can't be
# opened via os.open on this platform, per the module docstring) — the COMPATIBLE
# tier here comes from operating on DirectoryHandle.path (verified once, at
# open time) rather than a kernel-pinned descriptor, not from needing new
# Win32 calls for every record operation.


def open_child_directory(
    parent: DirectoryHandle, name: str, *, create: bool
) -> Optional[DirectoryHandle]:
    from ..paths import assert_unaliased_directory

    child_path = parent.path / name
    try:
        assert_unaliased_directory(child_path)
    except FileNotFoundError:
        if not create:
            return None
        child_path.mkdir(mode=0o700, exist_ok=True)
        assert_unaliased_directory(child_path)
    return open_directory(child_path)


def read_bounded_regular(parent: DirectoryHandle, name: str, *, max_bytes: int) -> Optional[bytes]:
    from ..paths import read_open_flags

    target = parent.path / name
    try:
        before = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError:
        return b""
    if not stat_module.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        return b""
    fd = -1
    try:
        fd = os.open(target, read_open_flags())
        current = os.fstat(fd)
        if (
            not stat_module.S_ISREG(current.st_mode)
            or current.st_size > max_bytes
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            return b""
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")
        if len(raw) > max_bytes or any(
            getattr(current, field) != getattr(after, field) for field in stable_fields
        ):
            return b""
        return raw
    except OSError:
        return b""
    finally:
        if fd >= 0:
            os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise OSError("short write while creating a record")
        offset += written


def atomic_create(parent: DirectoryHandle, name: str, data: bytes) -> bool:
    target_dir = parent.path
    target = target_dir / name
    temporary = target_dir / f".{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(
        os, "O_BINARY", 0
    )
    fd = -1
    linked = False
    try:
        fd = os.open(temporary, flags, 0o600)
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        try:
            os.link(temporary, target)
            linked = True
        except FileExistsError:
            return False
        return True
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            if linked:
                # The published record is valid; a leftover temp hard link
                # can be cleaned by an operator without weakening the lock.
                pass


def unlink(parent: DirectoryHandle, name: str) -> bool:
    try:
        os.unlink(parent.path / name)
    except FileNotFoundError:
        return False
    return True


def record_names(parent: DirectoryHandle, *, suffix: str, max_entries: int) -> list:
    names: list = []
    count = 0
    with os.scandir(parent.path) as entries:
        for entry in entries:
            count += 1
            if count > max_entries:
                raise OSError("record directory exceeds its entry limit")
            if entry.name.endswith(suffix):
                names.append(entry.name)
    return sorted(names)


# --- Process liveness probe (MutationLock stale-lock reaping, phase 4d) ----


def is_process_alive(pid: int) -> Optional[bool]:
    """Windows analogue of transaction._process_alive's POSIX ``os.kill(pid,
    0)`` probe.

    Deliberately does NOT call ``os.kill`` on Windows: unlike POSIX, where
    signal 0 is defined as a no-op existence probe, CPython's ``os.kill`` on
    Windows calls ``TerminateProcess`` for *any* signal value, including 0 —
    reusing it here would risk actually killing an unrelated process that
    recycled the stale lock's recorded PID, instead of merely checking
    whether it is still running. Uses ``OpenProcess`` (query-only access
    right) + ``GetExitCodeProcess`` instead, matching the three-way contract
    ``_process_alive`` already has: ``True`` (running), ``False`` (not
    running), ``None`` (couldn't determine either way).
    """

    if pid <= 0:
        return False
    try:
        import pywintypes
        import win32api
        import win32con
        import win32process
    except ImportError as exc:
        raise MissingDependencyError(
            "native Windows vault writes require pywin32; install it with "
            "'pip install pywin32'"
        ) from exc
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
    except pywintypes.error as exc:
        # ERROR_INVALID_PARAMETER: no such process. ERROR_ACCESS_DENIED: it
        # exists but this process can't query it -- still "alive" for our
        # purposes, same as _process_alive's PermissionError branch.
        if exc.winerror == 87:
            return False
        if exc.winerror == 5:
            return True
        return None
    try:
        exit_code = win32process.GetExitCodeProcess(handle)
    except pywintypes.error:
        return None
    finally:
        handle.Close()
    still_active = 259  # STILL_ACTIVE, per the Win32 GetExitCodeProcess docs
    return exit_code == still_active


def remap_write_error(path: Path, exc: OSError) -> OSError:
    """Return the exception a COMPATIBLE-tier write should actually raise
    for ``exc`` -- :class:`ControlledFolderAccessBlocked` with an actionable
    message when :func:`is_likely_cfa_block` matches, otherwise ``exc``
    itself unchanged.

    Callers should do ``raise windows_backend.remap_write_error(path, exc)
    from exc`` from an ``except OSError`` block wrapping a write. Only
    called from COMPATIBLE-tier code paths, so the ``winerror`` module
    import inside ``is_likely_cfa_block`` is always available there.
    """

    if is_likely_cfa_block(path, exc):
        return ControlledFolderAccessBlocked(
            getattr(exc, "errno", None),
            f"write to {path} was blocked, likely by Windows Defender's "
            "Controlled Folder Access -- add this process (or the vault "
            "folder) to the allow-list in Windows Security > App & browser "
            "control > Exploit protection > Controlled folder access, or "
            "move the vault out of a CFA-protected default folder "
            "(Documents/Desktop/Pictures/Videos/Music/Favorites)",
        )
    return exc
