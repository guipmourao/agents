"""POSIX dir_fd-relative directory operations.

Holds the directory-record primitives shared by ``legacy_lock.py``'s
directory walk (``open_root_fd``/``open_child_fd``/``fsync_dir``) and its
record read/write (``read_bounded_regular``/``atomic_create``/``unlink``/
``record_names``), plus (as of transaction.py's port, phase 4a)
``transaction.py``'s runtime-directory-chain walk
(``open_lock_root_fd``/``open_lock_parent_from_root_fd``/
``open_lock_parent_fd``) — moved here verbatim from their previous private,
file-specific forms so ``hostplatform.dirops`` has one POSIX implementation
and one Windows implementation to dispatch between, instead of each file
branching on ``os.name`` itself.

The rest of ``transaction.py``'s write path (``MutationLock``'s body, the
runtime/operation store, the content-write path) is not ported yet — see the
port plan's phase 4b-4g. Moving these three functions here does not, by
itself, enable anything on Windows: ``MutationLock`` still performs dozens of
its own raw dir_fd-relative calls directly in its body.
"""

from __future__ import annotations

import errno
import os
import stat as stat_module
import uuid
from typing import Optional

from .capability import PlatformCapability, strict_capability


def capability() -> Optional[PlatformCapability]:
    """STRICT capability if this host supports dir_fd confinement, else None."""

    from ..paths import supports_confined_dirfd

    if supports_confined_dirfd():
        return strict_capability()
    return None


def supports_confined_runtime() -> bool:
    """The primitive set ``legacy_lock.py``'s own directory walk needs.

    Intentionally checked separately from ``paths.supports_confined_dirfd``
    and ``transaction._require_lock_dirfd_support`` — each site requires a
    slightly different primitive set, and unifying them would blur which
    site actually needs which guarantee (see ``paths.py``'s
    ``supports_confined_dirfd`` docstring).
    """

    required_dir_fd = (os.open, os.mkdir, os.stat, os.unlink, os.link)
    return (
        os.name != "nt"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.listdir in os.supports_fd
        and all(function in os.supports_dir_fd for function in required_dir_fd)
        and os.stat in os.supports_follow_symlinks
        and os.link in os.supports_follow_symlinks
    )


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW


def fsync_dir(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EROFS}:
            raise


def open_root_fd(vault_root) -> int:
    if not supports_confined_runtime():
        raise OSError(errno.ENOTSUP, "this platform lacks no-follow directory-FD operations")
    descriptor = os.open(vault_root, _directory_flags())
    metadata = os.fstat(descriptor)
    if not stat_module.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise NotADirectoryError(errno.ENOTDIR, "selected vault is not a directory")
    return descriptor


def open_child_fd(parent_descriptor: int, name: str, *, create: bool) -> Optional[int]:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
            fsync_dir(parent_descriptor)
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
        except FileExistsError:
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)

    metadata = os.fstat(descriptor)
    if not stat_module.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise NotADirectoryError(errno.ENOTDIR, f"runtime component is not a directory: {name}")
    return descriptor


def read_bounded_regular(descriptor: int, name: str, *, max_bytes: int) -> Optional[bytes]:
    try:
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        return b""
    if not stat_module.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        return b""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    opened = -1
    try:
        opened = os.open(name, flags, dir_fd=descriptor)
        current = os.fstat(opened)
        if (
            not stat_module.S_ISREG(current.st_mode)
            or current.st_size > max_bytes
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            return b""
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(opened, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(opened)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")
        if len(raw) > max_bytes or any(
            getattr(current, field) != getattr(after, field) for field in stable_fields
        ):
            return b""
        return raw
    except OSError:
        return b""
    finally:
        if opened >= 0:
            os.close(opened)


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("short write while creating a record")
        offset += written


def atomic_create(descriptor: int, name: str, data: bytes) -> bool:
    temporary = f".{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    file_descriptor = -1
    linked = False
    try:
        file_descriptor = os.open(temporary, flags, 0o600, dir_fd=descriptor)
        _write_all(file_descriptor, data)
        os.fsync(file_descriptor)
        os.close(file_descriptor)
        file_descriptor = -1
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
                follow_symlinks=False,
            )
            linked = True
        except FileExistsError:
            return False
        fsync_dir(descriptor)
        return True
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        except OSError:
            if linked:
                # The published record is valid; an invisible temp hard link
                # can be cleaned by an operator without weakening the lock.
                pass


def unlink(descriptor: int, name: str) -> bool:
    """Remove ``name``. Callers are responsible for fsync'ing the parent
    directory afterward (``hostplatform.dirops.unlink`` does this uniformly for
    both backends, since Windows has no equivalent durability primitive to
    call unconditionally the way POSIX does)."""

    try:
        os.unlink(name, dir_fd=descriptor)
    except FileNotFoundError:
        return False
    return True


def record_names(descriptor: int, *, suffix: str, max_entries: int) -> list[str]:
    names: list[str] = []
    count = 0
    with os.scandir(descriptor) as entries:
        for entry in entries:
            count += 1
            if count > max_entries:
                raise OSError("record directory exceeds its entry limit")
            if entry.name.endswith(suffix):
                names.append(entry.name)
    return sorted(names)


# --- transaction.py's runtime-directory-chain walk (phase 4a) --------------


def supports_transaction_lock_dirfd() -> bool:
    """The primitive set ``transaction.py``'s ``MutationLock`` needs.

    Kept separate from :func:`supports_confined_runtime` (legacy_lock.py) and
    ``paths.supports_confined_dirfd`` on purpose — each site requires a
    slightly different primitive set (this one needs ``os.rename``/
    ``os.rmdir``, unlike the other two); see ``paths.supports_confined_dirfd``'s
    docstring for why these three checks intentionally stay separate.
    """

    required = (os.open, os.mkdir, os.stat, os.unlink, os.rmdir, os.rename)
    return (
        os.name != "nt"
        and bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and all(function in os.supports_dir_fd for function in required)
        and os.stat in os.supports_follow_symlinks
    )


def _lock_walk_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def open_lock_root_fd(vault_root) -> int:
    """Pin the canonical vault directory itself without following an alias."""

    return os.open(vault_root, _lock_walk_flags())


def open_lock_parent_from_root_fd(
    root_fd: int, components: tuple[str, ...], *, create: bool
) -> int:
    """Walk a runtime directory chain from a retained vault-root descriptor."""

    current = os.dup(root_fd)
    try:
        for component in components:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                    try:
                        os.fsync(current)
                    except OSError:
                        pass
                except FileExistsError:
                    pass
            following = os.open(component, _lock_walk_flags(), dir_fd=current)
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def open_lock_parent_fd(
    vault_root, components: tuple[str, ...], *, create: bool
) -> int:
    """Open a runtime directory chain without following any component alias."""

    root_fd = open_lock_root_fd(vault_root)
    try:
        return open_lock_parent_from_root_fd(root_fd, components, create=create)
    finally:
        os.close(root_fd)


# --- transaction.py's process-exclusivity lock (phase 4b) ------------------
#
# Windows counterpart: hostplatform.windows_backend.try_acquire_exclusive /
# release_exclusive (LockFileEx on a CreateFileW directory handle), built in
# an earlier phase. Nothing wires the two together yet -- MutationLock's body
# still only ever hands these functions a raw POSIX fd; that wiring is
# phase 4d, not this one.


def try_vault_advisory_lock(root_fd: int) -> bool:
    """Try to serialize the vault inode across runtime namespace replacement."""

    try:
        fcntl_module = __import__("fcntl")
        lock_ex = int(getattr(fcntl_module, "LOCK_EX"))
        lock_nb = int(getattr(fcntl_module, "LOCK_NB"))
        flock = getattr(fcntl_module, "flock")
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        raise OSError(
            errno.ENOTSUP,
            "process-lifetime vault locking requires fcntl.flock on WSL/Linux or macOS",
        ) from exc
    try:
        flock(root_fd, lock_ex | lock_nb)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
            return False
        raise
    return True


class LockIdentityChanged(RuntimeError):
    """A pinned lock directory no longer owns its public parent entry."""


def _lock_walk_at_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def open_lock_directory_at(parent_fd: int, name: str) -> int:
    """Open one lock directory relative to its already pinned parent."""

    return os.open(name, _lock_walk_at_flags(), dir_fd=parent_fd)


def lock_entry_matches(parent_fd: int, name: str, lock_fd: int) -> bool:
    """Return whether ``name`` still denotes the pinned directory."""

    try:
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        pinned = os.fstat(lock_fd)
    except OSError:
        return False
    return (
        stat_module.S_ISDIR(entry.st_mode)
        and entry.st_dev == pinned.st_dev
        and entry.st_ino == pinned.st_ino
    )


def read_lock_owner_at(lock_fd: int, *, limit: int = 64 * 1024) -> Optional[bytes]:
    """Read a bounded regular owner record through a pinned lock descriptor.

    Returns the raw bytes, or ``None`` on any failure (missing, wrong type,
    oversized, or a race during the read) -- callers own JSON parsing.
    """

    try:
        before = os.stat("owner.json", dir_fd=lock_fd, follow_symlinks=False)
    except OSError:
        return None
    if not stat_module.S_ISREG(before.st_mode) or before.st_size > limit:
        return None
    flags = (
        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open("owner.json", flags, dir_fd=lock_fd)
        opened = os.fstat(descriptor)
        if (
            not stat_module.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size > limit
        ):
            os.close(descriptor)
            return None
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            return None
        return raw
    except OSError:
        return None


def write_lock_owner_at(lock_fd: int, data: bytes) -> None:
    """Atomically install an owner record inside a pinned lock directory."""

    temporary = f".owner.json.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(temporary, flags, 0o600, dir_fd=lock_fd)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary, "owner.json", src_dir_fd=lock_fd, dst_dir_fd=lock_fd)
        os.fsync(lock_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=lock_fd)
        except FileNotFoundError:
            pass


def remove_lock_directory_at(parent_fd: int, name: str, lock_fd: int) -> None:
    """Remove only the pinned lock and never a replacement at its public name."""

    if not lock_entry_matches(parent_fd, name, lock_fd):
        raise LockIdentityChanged(f"lock directory identity changed: {name}")
    try:
        os.unlink("owner.json", dir_fd=lock_fd)
    except FileNotFoundError:
        pass
    try:
        os.fsync(lock_fd)
    except OSError:
        pass
    if not lock_entry_matches(parent_fd, name, lock_fd):
        raise LockIdentityChanged(f"lock directory identity changed: {name}")
    os.rmdir(name, dir_fd=parent_fd)
    try:
        os.fsync(parent_fd)
    except OSError:
        pass


def release_vault_advisory_lock(root_fd: int) -> None:
    """Release an advisory lock previously acquired on the vault descriptor."""

    try:
        fcntl_module = __import__("fcntl")
        flock = getattr(fcntl_module, "flock")
        lock_un = int(getattr(fcntl_module, "LOCK_UN"))
        flock(root_fd, lock_un)
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        # Closing the final open file description also releases flock.  The
        # explicit unlock is best-effort so release cannot leak descriptors.
        pass
