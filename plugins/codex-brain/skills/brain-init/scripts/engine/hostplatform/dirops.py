"""OS-dispatching, directory-relative operations for record-style runtime
directories — today used by ``legacy_lock.py``'s own directory walk
(``.vault-meta/locks/*.lock``), and the intended landing spot for
``transaction.py``'s similarly-shaped runtime-directory walk once that file
is ported.

Callers should use only the functions here for anything that used to be a
dir_fd-relative POSIX call — never call :mod:`hostplatform.posix_backend` or
:mod:`hostplatform.windows_backend` directly, and never branch on ``os.name``,
outside this module and ``paths.capability_for``.

Note what this module does *not* cover: process-exclusivity locking
(``fcntl.flock`` / ``LockFileEx``) is owned by ``transaction.MutationLock``,
not by anything here — ``legacy_lock.py``'s write commands
(``acquire``/``release``/``clear-stale``) call into ``MutationLock`` directly
via ``_serialized_lock_directory`` and stay POSIX-only until that lock is
ported. This module only covers the parts of the directory walk that
``legacy_lock.py`` owns itself: opening/creating runtime directories and
reading/writing/listing the record files inside them, used by
``list``/``peek``/``_validate_target_path`` today regardless of the lock
port's status.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class PinnedDirectory:
    """A directory identity pinned for the duration of an operation.

    POSIX: ``fd`` is a real dir_fd opened with ``O_DIRECTORY|O_NOFOLLOW``,
    kernel-pinned for the whole operation (STRICT tier); ``path`` is
    informational only and never used to re-derive an operation.

    Windows: ``fd`` is ``None``; ``win_handle`` holds the win32 directory
    handle plus the identity captured at open time. Every child operation
    re-derives its full path from ``path`` (COMPATIBLE tier — see
    ``windows_backend``'s module docstring for why this is a narrower, not
    eliminated, TOCTOU window compared to STRICT).
    """

    path: Path
    fd: Optional[int] = None
    win_handle: object = None


def open_root(vault_root) -> PinnedDirectory:
    root = Path(vault_root)
    if os.name != "nt":
        from . import posix_backend

        return PinnedDirectory(path=root, fd=posix_backend.open_root_fd(root))
    from . import windows_backend

    return PinnedDirectory(path=root, win_handle=windows_backend.open_directory(root))


def open_child(parent: PinnedDirectory, name: str, *, create: bool) -> Optional[PinnedDirectory]:
    if parent.fd is not None:
        from . import posix_backend

        fd = posix_backend.open_child_fd(parent.fd, name, create=create)
        return None if fd is None else PinnedDirectory(path=parent.path / name, fd=fd)
    from . import windows_backend

    handle = windows_backend.open_child_directory(parent.win_handle, name, create=create)
    return None if handle is None else PinnedDirectory(path=parent.path / name, win_handle=handle)


def close(directory: PinnedDirectory) -> None:
    if directory.fd is not None:
        os.close(directory.fd)
        return
    from . import windows_backend

    windows_backend.close_directory(directory.win_handle)


def fsync(directory: PinnedDirectory) -> None:
    if directory.fd is not None:
        from . import posix_backend

        posix_backend.fsync_dir(directory.fd)
    # Windows directory-entry durability semantics differ enough that this
    # stays a no-op there too, matching transaction.py's existing
    # _fsync_directory no-op on os.name == "nt".


def stat_component(directory: PinnedDirectory, name: str) -> os.stat_result:
    """lstat a single child of ``directory`` without following it.

    POSIX: dir_fd-relative, kernel-pinned. Windows: plain ``os.lstat`` on the
    joined path — this already returns a correct reparse tag on Windows (see
    ``paths.is_name_surrogate``), so no Win32 call is needed for this one.
    """

    if directory.fd is not None:
        return os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
    return os.lstat(directory.path / name)


def read_bounded_regular(directory: PinnedDirectory, name: str, *, max_bytes: int) -> Optional[bytes]:
    if directory.fd is not None:
        from . import posix_backend

        return posix_backend.read_bounded_regular(directory.fd, name, max_bytes=max_bytes)
    from . import windows_backend

    return windows_backend.read_bounded_regular(directory.win_handle, name, max_bytes=max_bytes)


def atomic_create(directory: PinnedDirectory, name: str, data: bytes) -> bool:
    if directory.fd is not None:
        from . import posix_backend

        return posix_backend.atomic_create(directory.fd, name, data)
    from . import windows_backend

    return windows_backend.atomic_create(directory.win_handle, name, data)


def unlink(directory: PinnedDirectory, name: str) -> bool:
    if directory.fd is not None:
        from . import posix_backend

        removed = posix_backend.unlink(directory.fd, name)
    else:
        from . import windows_backend

        removed = windows_backend.unlink(directory.win_handle, name)
    if removed:
        fsync(directory)
    return removed


def record_names(directory: PinnedDirectory, *, suffix: str, max_entries: int) -> List[str]:
    if directory.fd is not None:
        from . import posix_backend

        return posix_backend.record_names(directory.fd, suffix=suffix, max_entries=max_entries)
    from . import windows_backend

    return windows_backend.record_names(directory.win_handle, suffix=suffix, max_entries=max_entries)
