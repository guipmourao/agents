"""Recoverable, operation-level vault transactions.

Multi-file filesystem updates cannot be truly atomic on common filesystems.
This module therefore provides a stronger, honest contract: one process-held
mutation lock, precondition hashes, a durable journal, atomic per-file replace,
and deterministic rollback/recovery for the complete operation.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import socket
import stat
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

from .json_utils import parse_finite_json_float
from .paths import (
    VaultSelectionError,
    assert_unaliased_directory,
    assert_within,
    canonical,
    capability_for,
    directory_open_flags,
    is_name_surrogate,
    is_same_object,
    read_open_flags,
    supports_confined_dirfd,
)
from .hostplatform.capability import GuaranteeTier


BUNDLE_SCHEMA = "codex-brain.transaction.v1"
RESULT_SCHEMA = "codex-brain.transaction-result.v1"
JOURNAL_SCHEMA = "codex-brain.transaction-journal.v1"
OPERATION_TYPES = {
    "base",
    "save",
    "ingest",
    "autoresearch",
    "fold",
    "canvas",
    "lint-fix",
    "markdown",
    "migration",
    "setup",
    "capture",
    "configuration",
    "generic",
}

# Transaction journals, host locks, derived indexes, queues, and hook state are
# implementation-owned.  A user-authored bundle must never be able to replace
# them, even when it uses the otherwise intentionally broad ``generic`` type.
_RESERVED_WRITE_PATHS = {
    ".git",
    ".vault-meta/transactions",
    ".vault-meta/mutation.lock",
    ".vault-meta/locks",
    ".vault-meta/capture",
    ".vault-meta/chunks",
    ".vault-meta/bm25",
    ".vault-meta/orchestration",
    ".vault-meta/hook.log",
}
_RESERVED_WRITE_PREFIXES = (
    # Process-owned lock and compatibility-lock families.  Prefix matching also
    # protects their quarantine/reaper variants (for example ``.address.lock.d``).
    ".vault-meta/.address.lock",
    ".vault-meta/.bm25.lock",
    ".vault-meta/.embed-cache.lock",
    ".vault-meta/.tiling.lock",
    ".vault-meta/.transport",
    ".vault-meta/.wiki-lock.meta",
    # Derived cache families and their per-process temporary files.
    ".vault-meta/embed-cache",
    ".vault-meta/tiling-cache",
    ".vault-meta/transport",
)

# Every workflow type has a declared content domain. ``generic`` is
# intentionally wiki-only: an operation type is an authority boundary, not
# merely an audit label.
_WIKI_ONLY_OPERATIONS = {
    "save",
    "lint-fix",
    "markdown",
    "generic",
}
_WIKI_AND_RAW_OPERATIONS = {"ingest", "autoresearch"}
_MANAGED_METADATA_PATHS = {
    ".raw/.manifest.json",
    ".vault-meta/address-counter.txt",
}
_MANAGED_REQUEST_OPERATIONS = {"ingest", "autoresearch"}
_DIRECT_MANAGED_METADATA_AUTHORITY = {
    ".raw/.manifest.json": {"setup", "migration"},
    ".vault-meta/address-counter.txt": {"setup"},
}
_BOOTSTRAP_COMMON_PATHS = {
    ".codex-brain.json",
    ".gitignore",
    ".obsidian/app.json",
    ".obsidian/appearance.json",
    ".obsidian/graph.json",
    ".obsidian/snippets/vault-colors.css",
    ".raw/.manifest.json",
    "inbox/.gitkeep",
    "wiki/hot.md",
    "wiki/index.md",
    "wiki/log.md",
    "wiki/meta/ledgers/claim-ledger.json",
    "wiki/meta/ledgers/source-ledger.json",
    "wiki/overview.md",
}
_SETUP_EXTENSION_PATHS = {
    ".vault-meta/address-counter.txt",
    ".vault-meta/legacy-pages.txt",
    ".vault-meta/tiling-thresholds.json",
}
_POLICY_ROOT_CASE = {
    ".codex-brain.json": ".codex-brain.json",
    ".git": ".git",
    ".obsidian": ".obsidian",
    ".raw": ".raw",
    ".vault-meta": ".vault-meta",
    "inbox": "inbox",
    "wiki": "wiki",
}
MAX_TRANSACTION_FILE_BYTES = 64 * 1024 * 1024
MAX_TRANSACTION_TOTAL_BYTES = 128 * 1024 * 1024
MAX_TRANSACTION_WRITES = 1024
MAX_TRANSACTION_PATH_BYTES = 1024
MAX_TRANSACTION_RUNTIME_JSON_BYTES = 8 * 1024 * 1024
MAX_TRANSACTION_BUNDLE_BYTES = (
    MAX_TRANSACTION_TOTAL_BYTES + MAX_TRANSACTION_RUNTIME_JSON_BYTES
)
MAX_TRANSACTION_RUNTIME_ENTRIES = 4096
MAX_PORTABLE_SIBLING_ENTRIES = 100_000
MAX_TRANSACTION_RUNTIME_TREE_ENTRIES = MAX_TRANSACTION_WRITES + 16
MAX_TRANSACTION_RUNTIME_TREE_DEPTH = 1
MAX_RECOVERY_BACKUP_BYTES = MAX_TRANSACTION_FILE_BYTES
MAX_RECOVERY_TOTAL_BACKUP_BYTES = MAX_TRANSACTION_TOTAL_BYTES


class TransactionError(RuntimeError):
    exit_code = 1

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TransactionConflict(TransactionError):
    exit_code = 75


class TransactionValidationError(TransactionError):
    exit_code = 2


class TransactionRecoveryError(TransactionError):
    exit_code = 3


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object while rejecting parser-dependent ambiguity."""

    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _strict_json_loads(value: str) -> Any:
    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-finite JSON number is not permitted: {constant}")

    return json.loads(
        value,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=reject_constant,
        parse_float=parse_finite_json_float,
    )


def _portable_name_key(value: str) -> str:
    """Return a post-casefold NFC key for cross-platform policy identity."""

    return unicodedata.normalize("NFC", value.casefold())


# Portable-vault write-path rules (enforced on every platform so an inspect
# verdict means the same thing everywhere).  ``:`` names an NTFS alternate data
# stream — invisible to directory enumeration, so the alias audit could never
# see it; the rest are Win32-invalid filename characters.  Mirrors the capture
# layer's filename policy (capture._RESERVED_WINDOWS_NAMES).
_UNPORTABLE_PATH_CHARACTERS = frozenset(':<>|?*"')
_RESERVED_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_operation_id(value: Any) -> str:
    """Return a filesystem-safe operation ID or fail closed."""

    if not isinstance(value, str) or not value:
        raise TransactionValidationError(
            "INVALID_OPERATION_ID", "operation_id is required"
        )
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    if (
        value in {".", ".."}
        or len(value) > 128
        or any(character not in allowed for character in value)
    ):
        raise TransactionValidationError(
            "INVALID_OPERATION_ID", "operation_id contains unsafe characters"
        )
    return value


def _normalize_vault_path(value: Any) -> str:
    """Validate the portable lexical form of one vault-relative path."""

    if not isinstance(value, str) or not value:
        raise TransactionValidationError(
            "INVALID_WRITE_PATH", "path must be a non-empty string"
        )
    if any(character in value for character in ("\x00", "\n", "\r", "\\")):
        raise TransactionValidationError(
            "INVALID_WRITE_PATH", "path contains a control character or backslash"
        )
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or value in {"", "."} or ".." in parsed.parts:
        raise TransactionValidationError(
            "INVALID_WRITE_PATH", f"path must stay in vault: {value}"
        )
    normalized = parsed.as_posix()
    if normalized != value:
        raise TransactionValidationError(
            "NONCANONICAL_WRITE_PATH", f"path must be normalized: {value}"
        )
    if unicodedata.normalize("NFC", normalized) != normalized:
        raise TransactionValidationError(
            "NONCANONICAL_UNICODE_PATH",
            f"path must use NFC Unicode normalization: {value}",
        )
    if len(normalized.encode("utf-8")) > MAX_TRANSACTION_PATH_BYTES:
        raise TransactionValidationError(
            "WRITE_PATH_TOO_LONG",
            f"path exceeds the {MAX_TRANSACTION_PATH_BYTES}-byte portability limit: {value}",
        )
    return normalized


def _assert_portable_write_path(value: str) -> None:
    """Reject NEW write destinations that portable filesystems cannot host.

    Applies only where a plan proposes writes — never to reads of existing
    vault content, so pre-existing files with historically accepted names stay
    readable, indexable, and recoverable.
    """

    if any(character in _UNPORTABLE_PATH_CHARACTERS for character in value):
        # ``:`` names an NTFS alternate data stream, invisible to directory
        # enumeration; the rest are Win32-invalid filename characters.
        raise TransactionValidationError(
            "UNPORTABLE_WRITE_PATH",
            f"path contains a character that is unsafe on portable filesystems: {value}",
        )
    for component in value.split("/"):
        if component in {"", ".", ".."}:
            # Structural defects; _normalize_vault_path owns their error codes.
            continue
        if component.endswith((".", " ")):
            # Win32 strips trailing dots/spaces at the syscall boundary, so such
            # a name silently aliases its stripped form — an alias class the
            # casefold audit cannot observe.  Reject it everywhere.
            raise TransactionValidationError(
                "UNPORTABLE_WRITE_PATH",
                f"path component may not end with a dot or space: {value}",
            )
        if component.split(".", 1)[0].upper() in _RESERVED_DEVICE_NAMES:
            raise TransactionValidationError(
                "UNPORTABLE_WRITE_PATH",
                f"path component is a reserved device name: {value}",
            )


def _safe_vault_path(vault_root: Path, value: Any) -> tuple[str, Path]:
    """Validate one canonical vault-relative path and reject symlink traversal."""

    normalized = _normalize_vault_path(value)
    parsed = PurePosixPath(normalized)

    root = canonical(vault_root)
    lexical = root.joinpath(*parsed.parts)
    try:
        assert_within(root, lexical)
    except VaultSelectionError as exc:
        raise TransactionValidationError(exc.code, str(exc)) from exc

    cursor = root
    for index, part in enumerate(parsed.parts):
        cursor = cursor / part
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise TransactionValidationError(
                "UNSAFE_VAULT_PATH", f"cannot inspect {normalized}: {exc}"
            ) from exc
        if is_name_surrogate(metadata):
            raise TransactionValidationError(
                "SYMLINK_WRITE_PATH",
                f"transaction paths may not traverse symlinks or junctions: {normalized}",
            )
        if index < len(parsed.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise TransactionValidationError(
                "UNSAFE_VAULT_PATH",
                f"parent component is not a directory: {normalized}",
            )
    return normalized, lexical


def _safe_directory(
    vault_root: Path,
    relative: str,
    *,
    create: bool,
) -> Path:
    """Return a non-symlink runtime directory confined to the vault."""

    normalized, directory = _safe_vault_path(vault_root, relative)
    root = canonical(vault_root)
    cursor = root
    for part in PurePosixPath(normalized).parts:
        cursor = cursor / part
        if not cursor.exists():
            if not create:
                return directory
            try:
                cursor.mkdir()
                _fsync_directory(cursor.parent)
            except FileExistsError:
                pass
            except OSError as exc:
                raise TransactionValidationError(
                    "UNSAFE_RUNTIME_PATH",
                    f"cannot create runtime directory {relative}: {exc}",
                ) from exc
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            raise TransactionValidationError(
                "UNSAFE_RUNTIME_PATH",
                f"cannot inspect runtime directory {relative}: {exc}",
            ) from exc
        if is_name_surrogate(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise TransactionValidationError(
                "UNSAFE_RUNTIME_PATH",
                f"runtime directory is not a safe directory: {relative}",
            )
        try:
            assert_within(root, cursor)
        except VaultSelectionError as exc:
            raise TransactionValidationError(exc.code, str(exc)) from exc
    return directory


def safe_transactions_root(vault_root: Path | str, *, create: bool = False) -> Path:
    """Return the confined transaction journal directory."""

    directory = _safe_directory(
        canonical(vault_root), ".vault-meta/transactions", create=create
    )
    if create and directory.is_dir():
        os.chmod(directory, 0o700)
    return directory


def _supports_confined_dirfd() -> bool:
    return supports_confined_dirfd()


def _open_parent_directory(
    vault_root: Path,
    relative: str,
    *,
    create: bool,
    root_fd: int | None = None,
    meta_fd: int | None = None,
) -> tuple[int, str]:
    """Open a target parent from the vault FD without following symlinks."""

    if root_fd is None and not _supports_confined_dirfd():
        # Callers gate on _supports_confined_dirfd(); this backstop turns a
        # future unguarded call into a clean failure instead of AttributeError.
        raise TransactionValidationError(
            "UNSAFE_VAULT_PATH",
            "directory-descriptor traversal is unavailable on this platform",
        )
    normalized = (
        _normalize_vault_path(relative)
        if root_fd is not None
        else _safe_vault_path(vault_root, relative)[0]
    )
    parts = PurePosixPath(normalized).parts
    flags = directory_open_flags()
    if meta_fd is not None and parts[0] == ".vault-meta":
        descriptor = os.dup(meta_fd)
        walk_parts = parts[1:-1]
    else:
        descriptor = (
            os.dup(root_fd)
            if root_fd is not None
            else os.open(canonical(vault_root), flags)
        )
        walk_parts = parts[:-1]
    try:
        for part in walk_parts:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _assert_no_existing_portable_alias(
    vault_root: Path,
    relative: str,
    *,
    root_fd: int | Path | None = None,
    meta_fd: int | Path | None = None,
) -> None:
    """Reject an existing sibling whose NFC(casefold) identity aliases a path.

    ``root_fd``/``meta_fd`` are a POSIX dir_fd (STRICT tier), a ``Path``
    (COMPATIBLE tier -- native Windows inside a held ``MutationLock``, where
    ``_RuntimeStore``'s fds are paths, not descriptors), or ``None``
    (standalone caller, e.g. dry-run/inspect). A ``Path`` is treated exactly
    like the already-existing degraded/no-dirfd branch below: it gets the
    same per-component ``assert_unaliased_directory`` re-check, since there
    is no kernel-pinned descriptor to lean on either way.
    """

    normalized = (
        _normalize_vault_path(relative)
        if isinstance(root_fd, int)
        else _safe_vault_path(vault_root, relative)[0]
    )
    flags = directory_open_flags()
    handle: int | Path
    if isinstance(root_fd, int):
        handle = os.dup(root_fd)
    elif isinstance(root_fd, Path):
        handle = root_fd
    elif _supports_confined_dirfd():
        try:
            handle = os.open(vault_root, flags)
        except FileNotFoundError:
            return
    else:
        # Degraded platforms (native Windows) walk by path.  _safe_vault_path
        # above already rejected symlink/junction components lexically; each
        # directory is re-checked immediately before enumeration below.
        handle = canonical(vault_root)
    try:
        for index, part in enumerate(PurePosixPath(normalized).parts):
            if isinstance(handle, Path):
                try:
                    assert_unaliased_directory(handle)
                except (FileNotFoundError, NotADirectoryError):
                    return
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise TransactionValidationError(
                            "SYMLINK_WRITE_PATH",
                            f"transaction paths may not traverse symlinks or junctions: {normalized}",
                        ) from exc
                    raise TransactionValidationError(
                        "UNSAFE_VAULT_PATH",
                        f"cannot enumerate siblings for {normalized}: {exc}",
                    ) from exc
            try:
                names: list[str] = []
                with os.scandir(handle) as entries:
                    for entry in entries:
                        names.append(entry.name)
                        if len(names) > MAX_PORTABLE_SIBLING_ENTRIES:
                            raise TransactionValidationError(
                                "VAULT_DIRECTORY_LIMIT",
                                f"directory containing {normalized} exceeds the portable-name audit limit",
                            )
            except FileNotFoundError:
                if isinstance(handle, Path):
                    return
                raise TransactionValidationError(
                    "UNSAFE_VAULT_PATH",
                    f"cannot enumerate siblings for {normalized}: directory vanished",
                ) from None
            except OSError as exc:
                raise TransactionValidationError(
                    "UNSAFE_VAULT_PATH",
                    f"cannot enumerate siblings for {normalized}: {exc}",
                ) from exc
            key = _portable_name_key(part)
            aliases = [name for name in names if _portable_name_key(name) == key]
            if any(name != part for name in aliases):
                alias = next(name for name in aliases if name != part)
                raise TransactionValidationError(
                    "CASEFOLD_PATH_ALIAS",
                    f"vault already contains a portable path alias for {normalized}: {alias}",
                )
            if part not in names or index == len(PurePosixPath(normalized).parts) - 1:
                return
            if isinstance(handle, Path):
                handle = handle / part
                continue
            try:
                if index == 0 and part == ".vault-meta" and meta_fd is not None:
                    child = os.dup(meta_fd)
                else:
                    child = os.open(part, flags, dir_fd=handle)
            except (FileNotFoundError, NotADirectoryError):
                return
            os.close(handle)
            handle = child
    finally:
        if isinstance(handle, int):
            os.close(handle)


def _safe_hash(
    vault_root: Path,
    relative: str,
    *,
    root_fd: int | Path | None = None,
    meta_fd: int | Path | None = None,
) -> str | None:
    # A Path root_fd (COMPATIBLE tier -- native Windows inside a held
    # MutationLock) takes the same degraded/path-validated branch as
    # root_fd=None below: there is no dir_fd to confine the open with either
    # way, so _open_parent_directory (STRICT-only) is never reached for it.
    if not isinstance(root_fd, int):
        normalized, path = _safe_vault_path(vault_root, relative)
    else:
        normalized = _normalize_vault_path(relative)
        path = vault_root.joinpath(*PurePosixPath(normalized).parts)
    if isinstance(root_fd, Path) or (root_fd is None and not _supports_confined_dirfd()):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TransactionValidationError(
                "UNSAFE_VAULT_PATH", f"cannot inspect {normalized}: {exc}"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise TransactionValidationError(
                "UNSAFE_VAULT_PATH",
                f"transaction target is not a regular file: {normalized}",
            )
        flags = read_open_flags()
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise TransactionValidationError(
                "UNSAFE_VAULT_PATH", f"cannot open {normalized}: {exc}"
            ) from exc
    else:
        try:
            parent_descriptor, leaf = _open_parent_directory(
                vault_root,
                normalized,
                create=False,
                root_fd=root_fd,
                meta_fd=meta_fd,
            )
        except FileNotFoundError:
            return None
        try:
            flags = read_open_flags()
            try:
                descriptor = os.open(leaf, flags, dir_fd=parent_descriptor)
            except FileNotFoundError:
                return None
        finally:
            os.close(parent_descriptor)

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise TransactionValidationError(
                "UNSAFE_VAULT_PATH",
                f"transaction target is not a regular file: {normalized}",
            )
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _safe_file_state(
    vault_root: Path,
    relative: str,
    *,
    max_bytes: int | None = None,
    root_fd: int | Path | None = None,
    meta_fd: int | Path | None = None,
) -> tuple[str | None, int | None]:
    """Read one regular file's digest and permission mode from one descriptor.

    A ``Path`` ``root_fd`` (COMPATIBLE tier) takes the degraded/path-based
    branch below, same as ``root_fd=None`` on a host without dir_fd support
    -- ``_open_parent_directory`` is STRICT-tier-only.
    """

    if not isinstance(root_fd, int):
        normalized, path = _safe_vault_path(vault_root, relative)
    else:
        normalized = _normalize_vault_path(relative)
        path = vault_root.joinpath(*PurePosixPath(normalized).parts)
    descriptor = -1
    parent_descriptor = -1
    try:
        if isinstance(root_fd, int) or (root_fd is None and _supports_confined_dirfd()):
            try:
                parent_descriptor, leaf = _open_parent_directory(
                    vault_root,
                    normalized,
                    create=False,
                    root_fd=root_fd,
                    meta_fd=meta_fd,
                )
                descriptor = os.open(leaf, read_open_flags(), dir_fd=parent_descriptor)
            except FileNotFoundError:
                return None, None
            except OSError as exc:
                raise TransactionValidationError(
                    "UNSAFE_VAULT_PATH", f"cannot open {normalized}: {exc}"
                ) from exc
        else:
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                return None, None
            if not stat.S_ISREG(metadata.st_mode):
                raise TransactionValidationError(
                    "UNSAFE_VAULT_PATH",
                    f"transaction target is not regular: {normalized}",
                )
            descriptor = os.open(path, read_open_flags())
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise TransactionValidationError(
                "UNSAFE_VAULT_PATH", f"transaction target is not regular: {normalized}"
            )
        if max_bytes is not None and before.st_size > max_bytes:
            raise TransactionValidationError(
                "TRANSACTION_FILE_TOO_LARGE",
                f"transaction target exceeds {max_bytes} bytes: {normalized}",
            )
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")
        if any(
            getattr(before, field) != getattr(after, field) for field in stable_fields
        ):
            raise TransactionConflict(
                "FILE_CHANGED_DURING_READ",
                f"{normalized} changed while it was inspected",
            )
        return digest.hexdigest(), _portable_file_mode(after.st_mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _portable_file_mode(st_mode: int) -> int:
    """Permission bits for plan hashing, normalized on Windows.

    The Windows CRT synthesizes 0o666/0o444 regardless of how the file was
    created, which would make ``approval_sha256`` and the inspect ``modes``
    output differ from every POSIX host for identical content.
    """

    if os.name == "nt":
        return 0o644
    return st_mode & 0o777


def read_vault_regular(
    vault_root: Path | str,
    relative: str,
    *,
    limit: int = 8 * 1024 * 1024,
    missing_ok: bool = True,
    root_fd: int | Path | None = None,
    meta_fd: int | Path | None = None,
) -> bytes | None:
    """Read one bounded regular file through a no-follow vault-relative path.

    A ``Path`` ``root_fd`` (COMPATIBLE tier) takes the degraded/path-based
    branch below, same as ``root_fd=None`` on a host without dir_fd support.
    """

    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise TransactionValidationError(
            "INVALID_READ_LIMIT", "read limit must be positive"
        )
    root = canonical(vault_root)
    if not isinstance(root_fd, int):
        normalized, path = _safe_vault_path(root, relative)
    else:
        normalized = _normalize_vault_path(relative)
        path = root.joinpath(*PurePosixPath(normalized).parts)
    descriptor = -1
    parent_descriptor = -1
    try:
        if isinstance(root_fd, int) or (root_fd is None and _supports_confined_dirfd()):
            try:
                parent_descriptor, leaf = _open_parent_directory(
                    root,
                    normalized,
                    create=False,
                    root_fd=root_fd,
                    meta_fd=meta_fd,
                )
                descriptor = os.open(leaf, read_open_flags(), dir_fd=parent_descriptor)
            except FileNotFoundError:
                if missing_ok:
                    return None
                raise TransactionValidationError(
                    "VAULT_FILE_MISSING", f"vault file is missing: {normalized}"
                )
            except OSError as exc:
                raise TransactionValidationError(
                    "UNSAFE_VAULT_PATH", f"cannot open {normalized}: {exc}"
                ) from exc
        else:
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                if missing_ok:
                    return None
                raise TransactionValidationError(
                    "VAULT_FILE_MISSING", f"vault file is missing: {normalized}"
                )
            if not stat.S_ISREG(metadata.st_mode):
                raise TransactionValidationError(
                    "UNSAFE_VAULT_PATH", f"vault file is not regular: {normalized}"
                )
            try:
                descriptor = os.open(path, read_open_flags())
            except OSError as exc:
                raise TransactionValidationError(
                    "UNSAFE_VAULT_PATH", f"cannot open {normalized}: {exc}"
                ) from exc

        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise TransactionValidationError(
                "UNSAFE_VAULT_PATH", f"vault file is not regular: {normalized}"
            )
        if metadata.st_size > limit:
            raise TransactionValidationError(
                "VAULT_FILE_TOO_LARGE", f"vault file exceeds read limit: {normalized}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(1024 * 1024, limit + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > limit:
                raise TransactionValidationError(
                    "VAULT_FILE_TOO_LARGE",
                    f"vault file exceeds read limit: {normalized}",
                )
        return b"".join(chunks)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _ensure_safe_parent(vault_root: Path, relative: str) -> Path:
    parent = PurePosixPath(relative).parent.as_posix()
    if parent == ".":
        return canonical(vault_root)
    return _safe_directory(vault_root, parent, create=True)


def _atomic_vault_write(
    vault_root: Path,
    relative: str,
    data: bytes,
    *,
    mode: int | None,
    root_fd: int | Path | None = None,
    meta_fd: int | Path | None = None,
) -> None:
    """Atomically replace a target through a no-follow vault-relative directory FD.

    A ``Path`` ``root_fd`` (COMPATIBLE tier) takes the degraded/path-based
    branch below, same as ``root_fd=None`` on a host without dir_fd support.
    """

    if not isinstance(root_fd, int):
        normalized, target = _safe_vault_path(vault_root, relative)
    else:
        normalized = _normalize_vault_path(relative)
        target = vault_root.joinpath(*PurePosixPath(normalized).parts)
    if isinstance(root_fd, Path) or (root_fd is None and not _supports_confined_dirfd()):
        _ensure_safe_parent(vault_root, normalized)
        _safe_vault_path(vault_root, normalized)
        atomic_write(target, data, mode=mode)
        return

    parent_descriptor, leaf = _open_parent_directory(
        vault_root,
        normalized,
        create=True,
        root_fd=root_fd,
        meta_fd=meta_fd,
    )
    temporary = f".{leaf}.txn-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor = -1
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_descriptor)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode if mode is not None else 0o600)
        os.replace(
            temporary,
            leaf,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent_descriptor)


def _confined_vault_unlink(
    vault_root: Path,
    relative: str,
    *,
    expected_sha256: str,
    root_fd: int | Path | None = None,
    meta_fd: int | Path | None = None,
) -> None:
    """Unlink one regular file without following a swapped parent path.

    Rollback of a create operation must remove only the file that the
    transaction wrote.  On platforms with directory-FD support, both the
    verification and unlink are anchored to a directory opened from the vault
    root.  The fallback repeats all path checks immediately before unlinking.
    A ``Path`` ``root_fd`` (COMPATIBLE tier) takes that same fallback branch.
    """

    if not isinstance(root_fd, int):
        normalized, target = _safe_vault_path(vault_root, relative)
    else:
        normalized = _normalize_vault_path(relative)
        target = vault_root.joinpath(*PurePosixPath(normalized).parts)
    if isinstance(root_fd, Path) or (root_fd is None and not _supports_confined_dirfd()):
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise TransactionRecoveryError(
                "ROLLBACK_TARGET_CHANGED",
                f"rollback target is not a regular file: {normalized}",
            )
        if (
            _safe_hash(vault_root, normalized, root_fd=None, meta_fd=None)
            != expected_sha256
        ):
            raise TransactionRecoveryError(
                "ROLLBACK_TARGET_CHANGED",
                f"rollback target bytes changed: {normalized}",
            )
        _safe_vault_path(vault_root, normalized)
        repeated = target.lstat()
        if not stat.S_ISREG(repeated.st_mode) or (repeated.st_dev, repeated.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise TransactionRecoveryError(
                "ROLLBACK_TARGET_CHANGED",
                f"rollback target changed during verification: {normalized}",
            )
        target.unlink()
        _fsync_directory(target.parent)
        return

    try:
        parent_descriptor, leaf = _open_parent_directory(
            vault_root,
            normalized,
            create=False,
            root_fd=root_fd,
            meta_fd=meta_fd,
        )
    except (FileNotFoundError, OSError) as exc:
        raise TransactionRecoveryError(
            "ROLLBACK_TARGET_CHANGED",
            f"rollback parent changed for {normalized}: {exc}",
        ) from exc
    descriptor = -1
    try:
        flags = read_open_flags()
        try:
            descriptor = os.open(leaf, flags, dir_fd=parent_descriptor)
        except OSError as exc:
            raise TransactionRecoveryError(
                "ROLLBACK_TARGET_CHANGED",
                f"cannot open rollback target {normalized}: {exc}",
            ) from exc
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise TransactionRecoveryError(
                "ROLLBACK_TARGET_CHANGED",
                f"rollback target is not a regular file: {normalized}",
            )
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected_sha256:
            raise TransactionRecoveryError(
                "ROLLBACK_TARGET_CHANGED",
                f"rollback target bytes changed: {normalized}",
            )
        current = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise TransactionRecoveryError(
                "ROLLBACK_TARGET_CHANGED",
                f"rollback target changed during verification: {normalized}",
            )
        os.unlink(leaf, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def bundle_sha256(value: Any) -> str:
    """Return the canonical approval hash for a transaction bundle."""

    return sha256_bytes(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )


def _canonical_json_hash(value: Any) -> str:
    return bundle_sha256(value)


def _prepared_projection(writes: Iterable[PreparedWrite]) -> list[dict[str, Any]]:
    return [
        {
            "path": write.relative_path,
            "write_mode": write.mode,
            "original_sha256": write.original_sha256,
            "original_mode": write.original_mode,
            "new_sha256": write.content_sha256,
            "new_mode": write.new_mode,
        }
        for write in writes
    ]


def _identity_from_stat(value: os.stat_result) -> dict[str, Any]:
    return {
        "state": "existing",
        "device": value.st_dev,
        "inode": value.st_ino,
    }


def _assert_no_portable_vault_leaf_alias_at(
    parent: int | Path,
    leaf: str,
    *,
    self_lstat: os.stat_result | None = None,
) -> None:
    try:
        leaf_size = len(leaf.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise TransactionValidationError(
            "INVALID_VAULT_ROOT_NAME",
            "vault root name must be a bounded NFC portable path component",
        ) from exc
    if (
        not leaf
        or leaf in {".", ".."}
        or unicodedata.normalize("NFC", leaf) != leaf
        or leaf_size > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in leaf)
    ):
        raise TransactionValidationError(
            "INVALID_VAULT_ROOT_NAME",
            "vault root name must be a bounded NFC portable path component",
        )
    count = 0
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                count += 1
                if count > MAX_PORTABLE_SIBLING_ENTRIES:
                    raise TransactionValidationError(
                        "VAULT_DIRECTORY_LIMIT",
                        "vault parent exceeds the portable-name audit limit",
                    )
                if entry.name != leaf and _portable_name_key(
                    entry.name
                ) == _portable_name_key(leaf):
                    if self_lstat is not None and _is_leaf_itself(
                        parent, entry.name, self_lstat
                    ):
                        # On case-insensitive filesystems (APFS, NTFS) the
                        # on-disk spelling of the vault itself can differ from
                        # the spelling the caller typed; that is the same
                        # object, not an alias.
                        continue
                    raise TransactionValidationError(
                        "CASEFOLD_PATH_ALIAS",
                        f"vault parent already contains a portable alias for {leaf}: {entry.name}",
                    )
    except OSError as exc:
        raise TransactionValidationError(
            "UNSAFE_VAULT_IDENTITY", f"cannot enumerate vault parent: {exc}"
        ) from exc


def _is_leaf_itself(parent: int | Path, name: str, self_lstat: os.stat_result) -> bool:
    """True when a differently spelled sibling entry IS the vault object.

    Deliberate tolerance: two directory entries naming the same object (a
    case-insensitive volume's single entry, or a same-parent bind mount) are
    one vault, so treating them as a CASEFOLD_PATH_ALIAS would reject the
    vault itself.  Distinct objects — including a junction pointing at the
    vault, whose reparse point lstats with its own identity — still flag.
    """

    try:
        if isinstance(parent, int):
            entry_stat = os.lstat(name, dir_fd=parent)
        else:
            entry_stat = os.lstat(parent / name)
    except OSError:
        return False
    return is_same_object(entry_stat, self_lstat)


def _assert_no_portable_vault_root_alias(vault_root: Path) -> None:
    # No self_lstat here: on case-insensitive filesystems os.lstat(vault_root)
    # resolves ANY casing, so for an absent root (init) it would misidentify
    # an alien same-key sibling as "the vault itself" and skip the rejection.
    # The self-entry skip is safe only where the vault object is already
    # descriptor-pinned (the MutationLock call sites).  Native Windows does
    # not need it here because canonical() case-normalizes existing paths.
    if _supports_confined_dirfd():
        try:
            parent_fd = os.open(vault_root.parent, directory_open_flags())
        except OSError as exc:
            raise TransactionValidationError(
                "UNSAFE_VAULT_IDENTITY", f"cannot pin vault parent: {exc}"
            ) from exc
        try:
            _assert_no_portable_vault_leaf_alias_at(parent_fd, vault_root.name)
        finally:
            os.close(parent_fd)
        return
    parent = canonical(vault_root).parent
    try:
        assert_unaliased_directory(parent)
    except OSError as exc:
        raise TransactionValidationError(
            "UNSAFE_VAULT_IDENTITY", f"cannot pin vault parent: {exc}"
        ) from exc
    _assert_no_portable_vault_leaf_alias_at(parent, vault_root.name)


def _path_mode_identity(vault: Path) -> dict[str, Any]:
    """Identity for degraded (non-dirfd) platforms, from lstat alone.

    Never opens a directory: the Windows CRT refuses ``os.open`` on
    directories regardless of flags.
    """

    try:
        value = assert_unaliased_directory(vault)
    except FileNotFoundError:
        parent = vault.parent
        try:
            parent_stat = assert_unaliased_directory(parent)
        except OSError as exc:
            raise TransactionValidationError(
                "UNSAFE_VAULT_IDENTITY", f"cannot identify vault directory: {exc}"
            ) from exc
        _require_stable_identity(parent_stat, parent)
        _assert_no_portable_vault_leaf_alias_at(parent, vault.name)
        try:
            os.lstat(vault)
        except FileNotFoundError:
            return {
                "state": "absent",
                "parent_device": parent_stat.st_dev,
                "parent_inode": parent_stat.st_ino,
                "leaf": vault.name,
            }
        except OSError as exc:
            raise TransactionValidationError(
                "UNSAFE_VAULT_IDENTITY", f"cannot identify vault directory: {exc}"
            ) from exc
        raise TransactionValidationError(
            "VAULT_IDENTITY_CHANGED", "vault appeared while its identity was read"
        )
    except OSError as exc:
        if exc.errno == errno.ENOTDIR:
            raise TransactionValidationError(
                "VAULT_NOT_DIRECTORY", f"vault is not a directory: {vault}"
            ) from exc
        raise TransactionValidationError(
            "UNSAFE_VAULT_IDENTITY", f"cannot identify vault directory: {exc}"
        ) from exc
    _require_stable_identity(value, vault)
    return _identity_from_stat(value)


def _require_stable_identity(value: os.stat_result, path: Path) -> None:
    if value.st_ino == 0:
        raise TransactionValidationError(
            "UNSAFE_VAULT_IDENTITY",
            f"filesystem does not expose stable file identity for {path} "
            "(FAT/exFAT/some network shares); use an NTFS volume or WSL",
        )


def _vault_object_identity(
    vault_root: Path | str, *, root_fd: int | Path | None = None
) -> dict[str, Any]:
    """Return a stable identity for an existing vault or its absent-root slot.

    A ``Path`` ``root_fd`` (COMPATIBLE tier, e.g. ``_RuntimeStore.root_fd``
    inside a held Windows ``MutationLock``) has no descriptor to ``fstat`` --
    it takes the same ``_path_mode_identity`` path as ``root_fd=None`` on a
    host without dir_fd support, evaluated at the pinned root path itself
    rather than re-deriving ``vault`` from scratch (they're the same path by
    construction, but this makes that explicit rather than assumed).
    """

    vault = canonical(vault_root)
    if isinstance(root_fd, int):
        return _identity_from_stat(os.fstat(root_fd))
    if isinstance(root_fd, Path) or not _supports_confined_dirfd():
        return _path_mode_identity(vault)
    try:
        descriptor = os.open(
            vault,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        parent = vault.parent
        parent_fd = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            parent_stat = os.fstat(parent_fd)
            _assert_no_portable_vault_leaf_alias_at(parent_fd, vault.name)
            try:
                os.stat(vault.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return {
                    "state": "absent",
                    "parent_device": parent_stat.st_dev,
                    "parent_inode": parent_stat.st_ino,
                    "leaf": vault.name,
                }
            raise TransactionValidationError(
                "VAULT_IDENTITY_CHANGED", "vault appeared while its identity was read"
            )
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise TransactionValidationError(
            "UNSAFE_VAULT_IDENTITY", f"cannot identify vault directory: {exc}"
        ) from exc
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISDIR(value.st_mode):
            raise TransactionValidationError(
                "VAULT_NOT_DIRECTORY", f"vault is not a directory: {vault}"
            )
        return _identity_from_stat(value)
    finally:
        os.close(descriptor)


def _valid_existing_vault_identity(value: Mapping[str, Any]) -> bool:
    return (
        set(value) == {"state", "device", "inode"}
        and value.get("state") == "existing"
        and isinstance(value.get("device"), int)
        and not isinstance(value.get("device"), bool)
        and isinstance(value.get("inode"), int)
        and not isinstance(value.get("inode"), bool)
    )


def _valid_absent_vault_identity(value: Mapping[str, Any], *, leaf: str) -> bool:
    return (
        set(value) == {"state", "parent_device", "parent_inode", "leaf"}
        and value.get("state") == "absent"
        and value.get("leaf") == leaf
        and isinstance(value.get("parent_device"), int)
        and not isinstance(value.get("parent_device"), bool)
        and isinstance(value.get("parent_inode"), int)
        and not isinstance(value.get("parent_inode"), bool)
    )


def plan_approval_sha256(
    vault_root: Path | str,
    expanded_bundle: Mapping[str, Any],
    prepared_writes: Iterable[PreparedWrite],
    *,
    vault_label: str | None = None,
    vault_identity: Mapping[str, Any] | None = None,
) -> str:
    """Bind a reviewed operation, vault, bytes, and file modes together."""

    return bundle_sha256(
        {
            "schema": "codex-brain.plan-approval.v3",
            "vault_root": vault_label
            if vault_label is not None
            else str(canonical(vault_root)),
            "vault_identity": dict(vault_identity)
            if vault_identity is not None
            else _vault_object_identity(vault_root),
            "expanded_bundle_sha256": _canonical_json_hash(expanded_bundle),
            "prepared_writes": _prepared_projection(prepared_writes),
        }
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.txn-", dir=path.parent
        )
    except OSError as exc:
        if os.name == "nt":
            from .hostplatform import windows_backend

            raise windows_backend.remap_write_error(path, exc) from exc
        raise
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp, mode)
        os.replace(temp, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        if os.name == "nt":
            from .hostplatform import windows_backend

            raise windows_backend.remap_write_error(path, exc) from exc
        raise
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, _json_bytes(value))


def _process_alive(pid: int) -> bool | None:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


class _LockIdentityChanged(RuntimeError):
    """A pinned lock directory no longer owns its public parent entry."""


def _require_lock_dirfd_support() -> None:
    """Fail closed unless the host supplies the dirfd primitives locks need.

    WSL/Linux and supported macOS Python builds provide this set.  Native
    Windows does not (yet, at this point in the port -- see phase 4g), so
    callers must use WSL rather than silently falling back to path-based lock
    operations that can follow a concurrently swapped alias.
    """

    from .hostplatform import posix_backend

    if not posix_backend.supports_transaction_lock_dirfd():
        raise _PlatformConfinementUnavailable(
            errno.ENOTSUP,
            "directory-descriptor lock confinement requires WSL/Linux or supported macOS",
        )


_UNSUPPORTED_PLATFORM_MESSAGE = (
    "vault writes require directory-descriptor confinement (WSL/Linux or "
    "supported macOS); on native Windows run this command inside WSL — "
    "read-only inspection and dry-runs work natively; if WSL itself "
    "misbehaves, see docs/windows-wsl.md"
)


class _PlatformConfinementUnavailable(OSError):
    """ENOTSUP raised by the platform capability gate itself.

    Distinct type so callers can map exactly this condition to
    UNSUPPORTED_PLATFORM without also swallowing a genuine EOPNOTSUPP that a
    filesystem returns on an otherwise supported host (Linux aliases the two
    errno values).
    """


#: Native Windows (COMPATIBLE tier) writes stay behind this env var until the
#: rollout plan's promotion criteria are met: Windows CI green through a
#: soak period, deterministic TOCTOU-injection tests, OneDrive/Controlled
#: Folder Access verified on a real machine, docs updated -- see the
#: "Rollout faseado" section of the port plan and docs/windows-wsl.md.
#: Without it, native Windows keeps today's exact behavior: hard refuse,
#: point to WSL. STRICT tier (POSIX/WSL/macOS) is never gated by this.
_WINDOWS_WRITE_OPT_IN_ENV_VAR = "CODEX_BRAIN_WINDOWS_WRITE"


def _windows_write_opted_in() -> bool:
    return os.environ.get(_WINDOWS_WRITE_OPT_IN_ENV_VAR) == "1"


def _require_write_platform(vault_root: Path | str) -> GuaranteeTier:
    """Resolve and validate the write tier for ``vault_root``, or raise.

    Called before any side effect (directory creation, backup staging) so a
    refused write leaves nothing behind -- both by the top-level entry
    points (``apply_bundle``, ``cli.py``'s init/adopt apply path) and by
    ``MutationLock.acquire()`` itself, so the two can never disagree about
    what this vault is allowed to do. Returns the resolved tier so
    ``MutationLock.acquire()`` doesn't need to recompute ``capability_for``.
    """

    tier = capability_for(vault_root).tier
    if tier is GuaranteeTier.STRICT:
        return tier
    if tier is GuaranteeTier.COMPATIBLE:
        if _windows_write_opted_in():
            return tier
        raise TransactionValidationError(
            "UNSUPPORTED_PLATFORM",
            _UNSUPPORTED_PLATFORM_MESSAGE
            + " (native Windows support exists but is opt-in while it's "
            f"rolled out: set {_WINDOWS_WRITE_OPT_IN_ENV_VAR}=1 to use it)",
        )
    raise TransactionValidationError(
        "UNSAFE_VAULT_IDENTITY",
        f"{canonical(vault_root)} is on a filesystem that cannot host vault writes safely",
    )


def _open_lock_parent_fd(
    vault_root: Path,
    components: tuple[str, ...],
    *,
    create: bool,
) -> int:
    """Open a runtime directory chain without following any component alias."""

    root_fd = _open_lock_root_fd(vault_root)
    try:
        return _open_lock_parent_from_root_fd(root_fd, components, create=create)
    finally:
        os.close(root_fd)


def _open_lock_root_fd(vault_root: Path) -> int:
    """Pin the canonical vault directory itself without following an alias.

    POSIX-only for now (phase 4a of the Windows port moved the walk itself
    into ``hostplatform.posix_backend`` for reuse, but ``MutationLock``'s body
    still performs its own raw dir_fd calls directly -- Windows support for
    this whole chain lands together in phase 4d/4g, not incrementally here).
    """

    _require_lock_dirfd_support()
    from .hostplatform import posix_backend

    return posix_backend.open_lock_root_fd(canonical(vault_root))


def _open_lock_parent_from_root_fd(
    root_fd: int,
    components: tuple[str, ...],
    *,
    create: bool,
) -> int:
    """Walk a runtime directory chain from a retained vault-root descriptor."""

    from .hostplatform import posix_backend

    return posix_backend.open_lock_parent_from_root_fd(root_fd, components, create=create)


def _try_vault_advisory_lock(root_fd: int) -> bool:
    """Try to serialize the vault inode across runtime namespace replacement.

    POSIX-only for now (phase 4b of the Windows port moved the body into
    ``hostplatform.posix_backend`` for reuse; the Windows counterpart
    already exists as ``hostplatform.windows_backend.try_acquire_exclusive``,
    built in an earlier phase, but nothing calls it yet -- ``MutationLock``
    still only ever hands this function a raw POSIX fd. Wiring the two
    together is phase 4d, not this one).
    """

    from .hostplatform import posix_backend

    return posix_backend.try_vault_advisory_lock(root_fd)


def _release_vault_advisory_lock(root_fd: int) -> None:
    """Release an advisory lock previously acquired on the vault descriptor."""

    from .hostplatform import posix_backend

    posix_backend.release_vault_advisory_lock(root_fd)


def _open_lock_directory_at(parent_fd: int, name: str) -> int:
    """Open one lock directory relative to its already pinned parent.

    POSIX-only for now (phase 4c of the Windows port moved these five
    lock-owner-record functions into ``hostplatform.posix_backend`` for
    reuse; ``MutationLock``'s body still only ever hands them raw POSIX fds
    -- Windows wiring is phase 4d).
    """

    from .hostplatform import posix_backend

    return posix_backend.open_lock_directory_at(parent_fd, name)


def _lock_entry_matches(parent_fd: int, name: str, lock_fd: int) -> bool:
    """Return whether ``name`` still denotes the pinned directory."""

    from .hostplatform import posix_backend

    return posix_backend.lock_entry_matches(parent_fd, name, lock_fd)


def _read_lock_owner_at(
    lock_fd: int, *, limit: int = 64 * 1024
) -> dict[str, Any] | None:
    """Read a bounded regular owner record through a pinned lock descriptor."""

    from .hostplatform import posix_backend

    raw = posix_backend.read_lock_owner_at(lock_fd, limit=limit)
    if raw is None:
        return None
    try:
        value = _strict_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_lock_owner_at(lock_fd: int, value: Mapping[str, Any]) -> None:
    """Atomically install an owner record inside a pinned lock directory."""

    from .hostplatform import posix_backend

    posix_backend.write_lock_owner_at(lock_fd, _json_bytes(dict(value)))


def _remove_lock_directory_at(parent_fd: int, name: str, lock_fd: int) -> None:
    """Remove only the pinned lock and never a replacement at its public name."""

    from .hostplatform import posix_backend

    try:
        posix_backend.remove_lock_directory_at(parent_fd, name, lock_fd)
    except posix_backend.LockIdentityChanged as exc:
        raise _LockIdentityChanged(str(exc)) from exc


@dataclass
class MutationLock:
    vault_root: Path
    timeout: float = 10.0
    stale_after: float = 3600.0
    poll_interval: float = 0.05
    force_stale_lock: bool = False
    expected_root_parent_identity: Mapping[str, Any] | None = None
    expected_vault_identity: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        self.vault_root = canonical(self.vault_root)
        self.path = self.vault_root / ".vault-meta" / "mutation.lock"
        self.token = uuid.uuid4().hex
        self.acquired = False
        self._root_parent_fd: int | None = None
        self._root_name = self.vault_root.name or "."
        self._root_fd: int | None = None
        self._parent_fd: int | None = None
        self._lock_fd: int | None = None
        self._advisory_locked = False
        # COMPATIBLE tier (native Windows) only -- see _acquire_compatible.
        # Left None on POSIX/STRICT tier for the whole lifetime of the lock.
        self._win_root_handle: Any = None
        self._win_lock_dir: Path | None = None

    @property
    def owner_path(self) -> Path:
        return self.path / "owner.json"

    def _owner(self, lock_fd: int) -> dict[str, Any] | None:
        return _read_lock_owner_at(lock_fd)

    def _may_reap_owner(
        self,
        now: float,
        owner: dict[str, Any] | None,
        *,
        fallback_mtime: float | None,
        process_alive: Any,
    ) -> bool:
        """Shared staleness policy behind both tiers' ``_may_reap*``.

        Kept as pure logic over an already-read owner record (plus a
        fallback mtime for the "owner record missing/unreadable" case) so
        STRICT and COMPATIBLE never risk drifting on what counts as stale --
        only how the owner record and mtime are obtained differs per tier.
        """

        if owner is None:
            # A missing or unreadable owner record is ambiguous: the creating
            # process may be paused between mkdir() and its atomic owner write.
            # Only the explicit recovery override may resolve that ambiguity.
            if not self.force_stale_lock or fallback_mtime is None:
                return False
            return now - fallback_mtime > self.stale_after
        started = owner.get("started_epoch")
        pid = owner.get("pid")
        host = owner.get("host")
        if not isinstance(started, (int, float)) or not isinstance(pid, int):
            return False
        age = now - float(started)
        if age <= self.stale_after:
            return False
        if self.force_stale_lock:
            return True
        if not isinstance(host, str) or host != socket.gethostname():
            return False
        alive = process_alive(pid)
        return alive is False

    def _may_reap(self, now: float, lock_fd: int) -> bool:
        owner = self._owner(lock_fd)
        fallback_mtime: float | None = None
        if owner is None:
            try:
                fallback_mtime = os.fstat(lock_fd).st_mtime
            except OSError:
                fallback_mtime = None
        return self._may_reap_owner(
            now, owner, fallback_mtime=fallback_mtime, process_alive=_process_alive
        )

    # --- COMPATIBLE tier (native Windows) -----------------------------------
    #
    # No dir_fd confinement is available at all on Windows (os.open cannot
    # even open a directory there -- see hostplatform.windows_backend's module
    # docstring), so this whole tier operates by full path instead of a
    # pinned descriptor chain, narrowing the TOCTOU window rather than
    # eliminating it -- the same COMPATIBLE-tier tradeoff already made
    # throughout this file's existing degraded branches (_atomic_vault_write,
    # _confined_vault_unlink, _path_mode_identity). Process-exclusivity still
    # needs a real OS primitive with crash-safe auto-release, which plain path
    # operations cannot provide -- that part uses
    # hostplatform.windows_backend's CreateFileW + LockFileEx directory
    # handle, not a marker file/directory alone.
    #
    # UNVERIFIED on a real Windows host as of this writing (no Windows CI has
    # run yet -- see docs/windows-wsl.md and the port plan's phase 8 rollout
    # rigor). Reachable only behind CODEX_BRAIN_WINDOWS_WRITE=1
    # (_windows_write_opted_in) -- default behavior on native Windows is
    # still today's hard refuse until the rollout plan's promotion criteria
    # are met and that gate is removed.

    def _owner_compatible(self, lock_dir: Path) -> dict[str, Any] | None:
        owner_path = lock_dir / "owner.json"
        try:
            before = owner_path.lstat()
        except OSError:
            return None
        if not stat.S_ISREG(before.st_mode) or before.st_size > 64 * 1024:
            return None
        descriptor = -1
        try:
            descriptor = os.open(owner_path, read_open_flags())
            current = os.fstat(descriptor)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_size > 64 * 1024
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            ):
                return None
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                raw = handle.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                return None
            value = _strict_json_loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _may_reap_compatible(self, now: float, lock_dir: Path) -> bool:
        owner = self._owner_compatible(lock_dir)
        fallback_mtime: float | None = None
        if owner is None:
            try:
                fallback_mtime = lock_dir.stat().st_mtime
            except OSError:
                fallback_mtime = None
        from .hostplatform import windows_backend

        return self._may_reap_owner(
            now,
            owner,
            fallback_mtime=fallback_mtime,
            process_alive=windows_backend.is_process_alive,
        )

    def _write_owner_compatible(self, lock_dir: Path, value: Mapping[str, Any]) -> None:
        data = _json_bytes(dict(value))
        temporary = lock_dir / f".owner.json.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except OSError as exc:
            from .hostplatform import windows_backend

            raise windows_backend.remap_write_error(temporary, exc) from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, lock_dir / "owner.json")
        except OSError as exc:
            from .hostplatform import windows_backend

            raise windows_backend.remap_write_error(lock_dir, exc) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _remove_lock_dir_compatible(self, lock_dir: Path, expected: os.stat_result) -> None:
        """Remove only the pinned lock and never a replacement at its public
        name -- the path-based analogue of ``_remove_lock_directory_at``."""

        try:
            (lock_dir / "owner.json").unlink()
        except FileNotFoundError:
            pass
        try:
            current = lock_dir.lstat()
        except FileNotFoundError as exc:
            raise _LockIdentityChanged(f"lock directory identity changed: {lock_dir}") from exc
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise _LockIdentityChanged(f"lock directory identity changed: {lock_dir}")
        lock_dir.rmdir()

    def _close_descriptors_compatible(self) -> None:
        from .hostplatform import windows_backend

        self._win_lock_dir = None
        if self._win_root_handle is not None:
            if self._advisory_locked:
                try:
                    windows_backend.release_exclusive(self._win_root_handle)
                except Exception:
                    # Best-effort, matching posix_backend.release_vault_advisory_lock:
                    # closing the handle below also releases the lock, and a
                    # failed explicit unlock (whatever exception type the
                    # underlying win32 call raises) must never block cleanup.
                    pass
                self._advisory_locked = False
            windows_backend.close_directory(self._win_root_handle)
            self._win_root_handle = None

    def _acquire_compatible(self) -> None:
        from .hostplatform import windows_backend

        deadline = time.monotonic() + max(0.0, self.timeout)
        meta_dir = self.vault_root / ".vault-meta"
        lock_dir = meta_dir / "mutation.lock"

        # Identity / alias checks reuse the already-existing path-based
        # (degraded) functions this file already ships for dry-run and
        # read-only use on native Windows -- no new identity logic here.
        if self.expected_vault_identity is not None:
            identity = _vault_object_identity(self.vault_root)
            expected_vault = self.expected_vault_identity
            if (
                not _valid_existing_vault_identity(expected_vault)
                or expected_vault["device"] != identity.get("device")
                or expected_vault["inode"] != identity.get("inode")
            ):
                raise TransactionValidationError(
                    "PLAN_CHANGED", "the selected vault object changed before locking"
                )
        _assert_no_portable_vault_root_alias(self.vault_root)

        try:
            root_handle = windows_backend.open_directory(self.vault_root)
        except OSError as exc:
            raise TransactionError("LOCK_FAILED", f"cannot pin vault root: {exc}") from exc
        self._win_root_handle = root_handle

        try:
            while True:
                if windows_backend.try_acquire_exclusive(root_handle):
                    break
                if time.monotonic() >= deadline:
                    raise TransactionConflict(
                        "LOCK_TIMEOUT", "vault mutation lock is held (owner pid=unknown)"
                    )
                time.sleep(self.poll_interval)
            self._advisory_locked = True

            try:
                meta_dir.mkdir(mode=0o700, exist_ok=True)
            except OSError as exc:
                remapped = windows_backend.remap_write_error(meta_dir, exc)
                raise TransactionError(
                    "LOCK_FAILED", f"cannot pin mutation lock parent: {remapped}"
                ) from remapped
            self.path = lock_dir

            while True:
                try:
                    lock_dir.mkdir(mode=0o700)
                except FileExistsError:
                    observed_owner = self._owner_compatible(lock_dir) or {}
                    if self._may_reap_compatible(time.time(), lock_dir):
                        try:
                            pre_rename_stat = lock_dir.lstat()
                        except OSError:
                            continue
                        quarantine = (
                            meta_dir / f"mutation.lock.reaping-{os.getpid()}-{uuid.uuid4().hex}"
                        )
                        try:
                            os.rename(lock_dir, quarantine)
                        except OSError:
                            continue
                        try:
                            self._remove_lock_dir_compatible(quarantine, pre_rename_stat)
                        except (_LockIdentityChanged, OSError):
                            # Unexpected contents remain confined under a
                            # unique quarantine name; never traversed
                            # recursively.
                            pass
                        continue
                    if time.monotonic() >= deadline:
                        raise TransactionConflict(
                            "LOCK_TIMEOUT",
                            "vault mutation lock is held "
                            f"(owner pid={observed_owner.get('pid', 'unknown')})",
                        )
                    time.sleep(self.poll_interval)
                    continue
                except OSError as exc:
                    raise TransactionError(
                        "LOCK_FAILED",
                        f"cannot create mutation lock: {windows_backend.remap_write_error(lock_dir, exc)}",
                    ) from exc

                self._win_lock_dir = lock_dir
                owner = {
                    "schema": "codex-brain.mutation-lock.v1",
                    "pid": os.getpid(),
                    "token": self.token,
                    "host": socket.gethostname(),
                    "started_epoch": time.time(),
                }
                try:
                    self._write_owner_compatible(lock_dir, owner)
                except Exception:
                    try:
                        expected = lock_dir.lstat()
                        self._remove_lock_dir_compatible(lock_dir, expected)
                    except OSError:
                        pass
                    raise
                self.acquired = True
                return
        except BaseException:
            if not self.acquired:
                self._close_descriptors_compatible()
            raise

    def _release_compatible(self) -> None:
        lock_dir = self._win_lock_dir
        try:
            if lock_dir is None:
                raise TransactionError(
                    "LOCK_OWNERSHIP_LOST", "mutation lock descriptors were lost"
                )
            owner = self._owner_compatible(lock_dir)
            if owner is None or not hmac.compare_digest(
                str(owner.get("token", "")), self.token
            ):
                raise TransactionError(
                    "LOCK_OWNERSHIP_LOST", "mutation lock owner changed before release"
                )
            try:
                expected = lock_dir.lstat()
            except OSError as exc:
                raise TransactionError(
                    "LOCK_RELEASE_FAILED", f"cannot release mutation lock: {exc}"
                ) from exc
            try:
                self._remove_lock_dir_compatible(lock_dir, expected)
            except _LockIdentityChanged as exc:
                raise TransactionError(
                    "LOCK_OWNERSHIP_LOST", "mutation lock path changed before release"
                ) from exc
            except OSError as exc:
                raise TransactionError(
                    "LOCK_RELEASE_FAILED", f"cannot release mutation lock: {exc}"
                ) from exc
        finally:
            self.acquired = False
            self._close_descriptors_compatible()

    def duplicate_parent_fd(self) -> int:
        """Duplicate the exact held ``.vault-meta`` namespace for a child lock."""

        if not self.acquired or self._parent_fd is None:
            raise TransactionError(
                "LOCK_NOT_HELD",
                "mutation lock parent is unavailable outside a held lock",
            )
        return os.dup(self._parent_fd)

    def duplicate_root_fd(self) -> int:
        """Duplicate the exact held vault-root namespace for target operations."""

        if not self.acquired or self._root_fd is None:
            raise TransactionError(
                "LOCK_NOT_HELD",
                "vault root is unavailable outside a held mutation lock",
            )
        return os.dup(self._root_fd)

    def open_metadata_dir_fd(self, relative: str, *, create: bool = False) -> int:
        """Open a no-follow directory below the exact held metadata namespace."""

        if not self.acquired or self._parent_fd is None:
            raise TransactionError(
                "LOCK_NOT_HELD",
                "metadata runtime is unavailable outside a held mutation lock",
            )
        normalized = _normalize_vault_path(relative)
        components = PurePosixPath(normalized).parts
        try:
            return _open_lock_parent_from_root_fd(
                self._parent_fd, components, create=create
            )
        except OSError as exc:
            raise TransactionValidationError(
                "UNSAFE_RUNTIME_PATH",
                f"cannot open confined metadata directory {normalized}: {exc}",
            ) from exc

    def assert_runtime_namespace_current(self) -> None:
        """Fail if the public metadata entry no longer names the pinned parent."""

        if self._win_root_handle is not None:
            # COMPATIBLE tier: a point-in-time lstat, not a pinned-descriptor
            # comparison -- narrower TOCTOU window, same tradeoff as the rest
            # of this tier (see _acquire_compatible's docstring).
            if not self.acquired or self._win_lock_dir is None:
                raise TransactionError(
                    "RUNTIME_NAMESPACE_CHANGED",
                    "the vault .vault-meta namespace changed while the mutation lock was held",
                )
            try:
                current = (self.vault_root / ".vault-meta").lstat()
            except OSError as exc:
                raise TransactionError(
                    "RUNTIME_NAMESPACE_CHANGED",
                    "the vault .vault-meta namespace changed while the mutation lock was held",
                ) from exc
            if is_name_surrogate(current) or not stat.S_ISDIR(current.st_mode):
                raise TransactionError(
                    "RUNTIME_NAMESPACE_CHANGED",
                    "the vault .vault-meta namespace changed while the mutation lock was held",
                )
            return
        if (
            not self.acquired
            or self._root_fd is None
            or self._parent_fd is None
            or not _lock_entry_matches(self._root_fd, ".vault-meta", self._parent_fd)
        ):
            raise TransactionError(
                "RUNTIME_NAMESPACE_CHANGED",
                "the vault .vault-meta namespace changed while the mutation lock was held",
            )

    def assert_vault_root_current(self) -> None:
        """Fail if the public vault entry no longer names the pinned root inode."""

        if self._win_root_handle is not None:
            if not self.acquired:
                raise TransactionError(
                    "VAULT_NAMESPACE_CHANGED",
                    "the selected vault root changed while the mutation lock was held",
                )
            try:
                current = self.vault_root.lstat()
            except OSError as exc:
                raise TransactionError(
                    "VAULT_NAMESPACE_CHANGED",
                    "the selected vault root changed while the mutation lock was held",
                ) from exc
            if is_name_surrogate(current) or not stat.S_ISDIR(current.st_mode):
                raise TransactionError(
                    "VAULT_NAMESPACE_CHANGED",
                    "the selected vault root changed while the mutation lock was held",
                )
            try:
                _assert_no_portable_vault_leaf_alias_at(
                    self.vault_root.parent, self.vault_root.name, self_lstat=current
                )
            except TransactionValidationError as exc:
                raise TransactionError(
                    "VAULT_NAMESPACE_CHANGED",
                    "the selected vault root gained a portable sibling alias",
                ) from exc
            return
        public_matches = False
        if self._root_fd is not None:
            try:
                public = os.stat(self.vault_root, follow_symlinks=False)
                pinned = os.fstat(self._root_fd)
                public_matches = (
                    stat.S_ISDIR(public.st_mode)
                    and public.st_dev == pinned.st_dev
                    and public.st_ino == pinned.st_ino
                )
            except OSError:
                pass
        if (
            not self.acquired
            or self._root_parent_fd is None
            or self._root_fd is None
            or not public_matches
            or not _lock_entry_matches(
                self._root_parent_fd, self._root_name, self._root_fd
            )
        ):
            raise TransactionError(
                "VAULT_NAMESPACE_CHANGED",
                "the selected vault root changed while the mutation lock was held",
            )
        try:
            assert self._root_parent_fd is not None
            _assert_no_portable_vault_leaf_alias_at(
                self._root_parent_fd,
                self._root_name,
                self_lstat=os.fstat(self._root_fd),
            )
        except TransactionValidationError as exc:
            raise TransactionError(
                "VAULT_NAMESPACE_CHANGED",
                "the selected vault root gained a portable sibling alias",
            ) from exc

    def _close_descriptors_strict(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        if self._parent_fd is not None:
            os.close(self._parent_fd)
            self._parent_fd = None
        if self._root_fd is not None:
            if self._advisory_locked:
                _release_vault_advisory_lock(self._root_fd)
                self._advisory_locked = False
            os.close(self._root_fd)
            self._root_fd = None
        if self._root_parent_fd is not None:
            os.close(self._root_parent_fd)
            self._root_parent_fd = None

    def acquire(self) -> None:
        """Acquire the mutation lock, dispatching to the tier this vault's
        volume supports -- STRICT (POSIX dir_fd confinement, unchanged) or
        COMPATIBLE (native Windows, see _acquire_compatible, gated behind
        CODEX_BRAIN_WINDOWS_WRITE=1 during rollout). Refuses outright on
        UNSAFE_REFUSED (e.g. FAT/exFAT). Delegates to the module-level
        ``_require_write_platform`` so this and every top-level entry point
        (``apply_bundle``, ``cli.py``) resolve the exact same tier for the
        exact same vault and can never disagree.
        """

        if self.acquired:
            return
        tier = _require_write_platform(self.vault_root)
        if tier is GuaranteeTier.COMPATIBLE:
            self._acquire_compatible()
            return
        self._acquire_strict()

    def release(self) -> None:
        if not self.acquired:
            return
        if self._win_root_handle is not None or self._win_lock_dir is not None:
            self._release_compatible()
            return
        self._release_strict()

    def _acquire_strict(self) -> None:
        deadline = time.monotonic() + max(0.0, self.timeout)
        try:
            root_fd = _open_lock_root_fd(self.vault_root)
        except OSError as exc:
            if isinstance(exc, _PlatformConfinementUnavailable):
                raise TransactionValidationError(
                    "UNSUPPORTED_PLATFORM", _UNSUPPORTED_PLATFORM_MESSAGE
                ) from exc
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise TransactionValidationError(
                    "SYMLINK_WRITE_PATH",
                    f"vault root is not a confined directory: {exc}",
                ) from exc
            raise TransactionError(
                "LOCK_FAILED", f"cannot pin vault root: {exc}"
            ) from exc
        self._root_fd = root_fd
        root_parent_flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            self._root_parent_fd = os.open(self.vault_root.parent, root_parent_flags)
        except OSError as exc:
            self._close_descriptors_strict()
            raise TransactionError(
                "LOCK_FAILED", f"cannot pin vault-root parent: {exc}"
            ) from exc
        if not _lock_entry_matches(self._root_parent_fd, self._root_name, root_fd):
            self._close_descriptors_strict()
            raise TransactionError(
                "LOCK_FAILED", "vault root changed while its descriptor was acquired"
            )
        try:
            _assert_no_portable_vault_leaf_alias_at(
                self._root_parent_fd,
                self._root_name,
                self_lstat=os.fstat(root_fd),
            )
        except TransactionValidationError:
            self._close_descriptors_strict()
            raise
        if self.expected_vault_identity is not None:
            expected_vault = self.expected_vault_identity
            pinned_root = os.fstat(root_fd)
            if (
                not _valid_existing_vault_identity(expected_vault)
                or expected_vault["device"] != pinned_root.st_dev
                or expected_vault["inode"] != pinned_root.st_ino
            ):
                self._close_descriptors_strict()
                raise TransactionValidationError(
                    "PLAN_CHANGED",
                    "the selected vault object changed before locking",
                )
        if self.expected_root_parent_identity is not None:
            expected = self.expected_root_parent_identity
            parent = os.fstat(self._root_parent_fd)
            if (
                not _valid_absent_vault_identity(expected, leaf=self._root_name)
                or expected["parent_device"] != parent.st_dev
                or expected["parent_inode"] != parent.st_ino
            ):
                self._close_descriptors_strict()
                raise TransactionValidationError(
                    "PLAN_CHANGED",
                    "the reviewed vault parent/leaf slot changed before locking",
                )
        self.path = self.vault_root / ".vault-meta" / "mutation.lock"
        try:
            while True:
                try:
                    advisory_acquired = _try_vault_advisory_lock(root_fd)
                except OSError as exc:
                    raise TransactionError(
                        "LOCK_FAILED", f"cannot acquire vault advisory lock: {exc}"
                    ) from exc
                if advisory_acquired:
                    break
                if time.monotonic() >= deadline:
                    raise TransactionConflict(
                        "LOCK_TIMEOUT",
                        "vault mutation lock is held (owner pid=unknown)",
                    )
                time.sleep(self.poll_interval)
            self._advisory_locked = True
            try:
                parent_fd = _open_lock_parent_from_root_fd(
                    root_fd, (".vault-meta",), create=True
                )
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise TransactionValidationError(
                        "SYMLINK_WRITE_PATH",
                        f"mutation lock runtime is not a confined directory: {exc}",
                    ) from exc
                raise TransactionError(
                    "LOCK_FAILED", f"cannot pin mutation lock parent: {exc}"
                ) from exc
            self._parent_fd = parent_fd
            while True:
                try:
                    os.mkdir("mutation.lock", mode=0o700, dir_fd=parent_fd)
                    try:
                        os.fsync(parent_fd)
                    except OSError:
                        pass
                except FileExistsError:
                    observed_owner: dict[str, Any] = {}
                    try:
                        existing_fd = _open_lock_directory_at(
                            parent_fd, "mutation.lock"
                        )
                    except FileNotFoundError:
                        continue
                    except OSError:
                        existing_fd = None
                    if existing_fd is not None:
                        try:
                            observed_owner = self._owner(existing_fd) or {}
                            if self._may_reap(time.time(), existing_fd):
                                if not _lock_entry_matches(
                                    parent_fd, "mutation.lock", existing_fd
                                ):
                                    raise TransactionError(
                                        "LOCK_OWNERSHIP_LOST",
                                        "mutation lock changed during stale-lock inspection",
                                    )
                                quarantine_name = f"mutation.lock.reaping-{os.getpid()}-{uuid.uuid4().hex}"
                                os.rename(
                                    "mutation.lock",
                                    quarantine_name,
                                    src_dir_fd=parent_fd,
                                    dst_dir_fd=parent_fd,
                                )
                                if not _lock_entry_matches(
                                    parent_fd, quarantine_name, existing_fd
                                ):
                                    raise TransactionError(
                                        "LOCK_OWNERSHIP_LOST",
                                        "mutation lock changed during stale-lock quarantine",
                                    )
                                try:
                                    _remove_lock_directory_at(
                                        parent_fd, quarantine_name, existing_fd
                                    )
                                except _LockIdentityChanged as exc:
                                    raise TransactionError(
                                        "LOCK_OWNERSHIP_LOST",
                                        "mutation lock quarantine changed before removal",
                                    ) from exc
                                except OSError:
                                    # Unexpected contents remain confined under a unique
                                    # quarantine name; they are never traversed recursively.
                                    pass
                                continue
                        finally:
                            os.close(existing_fd)
                    if time.monotonic() >= deadline:
                        raise TransactionConflict(
                            "LOCK_TIMEOUT",
                            "vault mutation lock is held "
                            f"(owner pid={observed_owner.get('pid', 'unknown')})",
                        )
                    time.sleep(self.poll_interval)
                    continue
                except OSError as exc:
                    raise TransactionError(
                        "LOCK_FAILED", f"cannot create mutation lock: {exc}"
                    ) from exc

                try:
                    lock_fd = _open_lock_directory_at(parent_fd, "mutation.lock")
                except OSError as exc:
                    raise TransactionError(
                        "LOCK_FAILED", f"cannot pin newly created mutation lock: {exc}"
                    ) from exc
                self._lock_fd = lock_fd
                if not _lock_entry_matches(parent_fd, "mutation.lock", lock_fd):
                    raise TransactionError(
                        "LOCK_OWNERSHIP_LOST",
                        "new mutation lock was concurrently replaced",
                    )
                owner = {
                    "schema": "codex-brain.mutation-lock.v1",
                    "pid": os.getpid(),
                    "token": self.token,
                    "host": socket.gethostname(),
                    "started_epoch": time.time(),
                }
                try:
                    _write_lock_owner_at(lock_fd, owner)
                except Exception:
                    try:
                        _remove_lock_directory_at(parent_fd, "mutation.lock", lock_fd)
                    except (OSError, _LockIdentityChanged):
                        pass
                    raise
                self.acquired = True
                return
        except BaseException:
            if not self.acquired:
                self._close_descriptors_strict()
            raise

    def _release_strict(self) -> None:
        if not self.acquired:
            return
        parent_fd, lock_fd = self._parent_fd, self._lock_fd
        try:
            if parent_fd is None or lock_fd is None:
                raise TransactionError(
                    "LOCK_OWNERSHIP_LOST", "mutation lock descriptors were lost"
                )
            owner = self._owner(lock_fd)
            if owner is None or not hmac.compare_digest(
                str(owner.get("token", "")), self.token
            ):
                raise TransactionError(
                    "LOCK_OWNERSHIP_LOST", "mutation lock owner changed before release"
                )
            try:
                _remove_lock_directory_at(parent_fd, "mutation.lock", lock_fd)
            except _LockIdentityChanged as exc:
                raise TransactionError(
                    "LOCK_OWNERSHIP_LOST", "mutation lock path changed before release"
                ) from exc
            except OSError as exc:
                raise TransactionError(
                    "LOCK_RELEASE_FAILED", f"cannot release mutation lock: {exc}"
                ) from exc
        finally:
            self.acquired = False
            self._close_descriptors_strict()

    def __enter__(self) -> "MutationLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()


def _runtime_component(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise TransactionRecoveryError(
            "CORRUPT_RUNTIME_STATE", f"invalid runtime path component: {value!r}"
        )
    return value


def _open_runtime_directory_at(
    parent: int | Path,
    name: str,
    *,
    create: bool,
    mode: int = 0o700,
) -> int | Path:
    """Open one runtime directory relative to a pinned parent, never following aliases.

    ``parent`` is a POSIX dir_fd (STRICT tier, kernel-pinned) or a ``Path``
    (COMPATIBLE tier / native Windows -- see MutationLock's
    ``_acquire_compatible``, which never sets a fd for callers to use here).
    The ``Path`` branch narrows the TOCTOU window instead of eliminating it,
    the same tradeoff already made throughout this file's other degraded
    branches (``_atomic_vault_write``, ``_path_mode_identity``, etc.).
    """

    component = _runtime_component(name)
    if isinstance(parent, Path):
        child = parent / component
        if create:
            try:
                child.mkdir(mode=mode)
            except FileExistsError:
                pass
        metadata = child.lstat()  # raises FileNotFoundError when create=False and absent
        if is_name_surrogate(metadata):
            raise OSError(
                errno.ELOOP, f"runtime entry is a symlink or junction: {component}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise TransactionRecoveryError(
                "CORRUPT_RUNTIME_STATE", f"runtime entry is not a directory: {component}"
            )
        return child
    if create:
        try:
            os.mkdir(component, mode=mode, dir_fd=parent)
            try:
                os.fsync(parent)
            except OSError:
                pass
        except FileExistsError:
            pass
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(component, flags, dir_fd=parent)
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise TransactionRecoveryError(
            "CORRUPT_RUNTIME_STATE", f"runtime entry is not a directory: {component}"
        )
    return descriptor


def _runtime_entry_metadata(directory: int | Path, name: str) -> os.stat_result | None:
    component = _runtime_component(name)
    try:
        if isinstance(directory, Path):
            return (directory / component).lstat()
        return os.stat(component, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _bounded_runtime_names(directory: int | Path, *, limit: int, label: str) -> list[str]:
    """Enumerate a runtime directory without materializing unbounded attacker state.

    ``os.scandir`` already accepts either a dir_fd or a path-like directly,
    so this needs no tier branch of its own.
    """

    names: list[str] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                names.append(_runtime_component(entry.name))
                if len(names) > limit:
                    raise TransactionRecoveryError(
                        "CORRUPT_RUNTIME_STATE",
                        f"{label} exceeds the {limit}-entry safety limit",
                    )
    except TransactionError:
        raise
    except OSError as exc:
        raise TransactionRecoveryError(
            "CORRUPT_RUNTIME_STATE", f"cannot enumerate {label}: {exc}"
        ) from exc
    return sorted(names)


def _read_runtime_bytes_at(
    directory: int | Path,
    name: str,
    *,
    label: str,
    limit: int,
    error_type: type[TransactionError] = TransactionRecoveryError,
) -> bytes:
    """Read a stable bounded regular runtime file from a pinned directory."""

    component = _runtime_component(name)
    if isinstance(directory, Path):
        target = directory / component
        descriptor = -1
        try:
            before = target.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise error_type(
                    "CORRUPT_RUNTIME_STATE", f"{label} is not a bounded regular file"
                )
            descriptor = os.open(target, read_open_flags())
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size > limit
            ):
                raise error_type(
                    "CORRUPT_RUNTIME_STATE", f"{label} changed before it was read"
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(descriptor, min(1024 * 1024, limit + 1 - total))
                if not block:
                    break
                chunks.append(block)
                total += len(block)
                if total > limit:
                    raise error_type(
                        "CORRUPT_RUNTIME_STATE", f"{label} exceeds its size limit"
                    )
            after = os.fstat(descriptor)
            stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")
            if any(getattr(opened, field) != getattr(after, field) for field in stable):
                raise error_type(
                    "CORRUPT_RUNTIME_STATE", f"{label} changed while it was read"
                )
            return b"".join(chunks)
        except TransactionError:
            raise
        except OSError as exc:
            raise error_type(
                "CORRUPT_RUNTIME_STATE", f"cannot read {label}: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    descriptor = -1
    try:
        before = os.stat(component, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise error_type(
                "CORRUPT_RUNTIME_STATE", f"{label} is not a bounded regular file"
            )
        descriptor = os.open(
            component,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size > limit
        ):
            raise error_type(
                "CORRUPT_RUNTIME_STATE", f"{label} changed before it was read"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(1024 * 1024, limit + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > limit:
                raise error_type(
                    "CORRUPT_RUNTIME_STATE", f"{label} exceeds its size limit"
                )
        after = os.fstat(descriptor)
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")
        if any(getattr(opened, field) != getattr(after, field) for field in stable):
            raise error_type(
                "CORRUPT_RUNTIME_STATE", f"{label} changed while it was read"
            )
        return b"".join(chunks)
    except TransactionError:
        raise
    except OSError as exc:
        raise error_type(
            "CORRUPT_RUNTIME_STATE", f"cannot read {label}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_runtime_json_at(
    directory_fd: int,
    name: str,
    *,
    label: str,
    limit: int = MAX_TRANSACTION_RUNTIME_JSON_BYTES,
) -> Any:
    raw = _read_runtime_bytes_at(
        directory_fd,
        name,
        label=label,
        limit=limit,
    )
    try:
        return _strict_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TransactionRecoveryError(
            "CORRUPT_RUNTIME_STATE", f"cannot parse {label}: {exc}"
        ) from exc


def _atomic_runtime_write_at(
    directory: int | Path,
    name: str,
    data: bytes,
    *,
    mode: int = 0o600,
) -> None:
    """Atomically replace one runtime file inside a pinned directory."""

    component = _runtime_component(name)
    if isinstance(directory, Path):
        target = directory / component
        temporary = directory / f".{component}.txn-{os.getpid()}-{uuid.uuid4().hex}"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                mode,
            )
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
                # Windows CRT synthesizes permission bits regardless of the
                # open() mode= argument (mode_verified=False for this tier,
                # per platform.capability) -- no fchmod call, matching how
                # _portable_file_mode already treats Windows modes elsewhere.
            os.replace(temporary, target)
            _fsync_directory(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return
    temporary = f".{component}.txn-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            mode,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.rename(
            temporary,
            component,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _atomic_runtime_json_at(
    directory: int | Path,
    name: str,
    value: Any,
    *,
    error_type: type[TransactionError] = TransactionRecoveryError,
) -> None:
    data = _json_bytes(value)
    if len(data) > MAX_TRANSACTION_RUNTIME_JSON_BYTES:
        raise error_type(
            "RUNTIME_STATE_TOO_LARGE",
            f"runtime JSON exceeds the {MAX_TRANSACTION_RUNTIME_JSON_BYTES}-byte recovery limit",
        )
    try:
        _atomic_runtime_write_at(directory, name, data)
    except OSError as exc:
        raise error_type(
            "CORRUPT_RUNTIME_STATE", f"cannot write confined runtime file {name}: {exc}"
        ) from exc


def _remove_pinned_runtime_tree_at(
    parent: int | Path,
    name: str,
    directory: int | Path,
    *,
    remaining: list[int] | None = None,
    depth: int = 0,
) -> None:
    """Recursively remove only a pinned runtime tree, never a replacement alias.

    COMPATIBLE tier (``directory`` is a ``Path``): the entry identity used
    for the pre-rmdir re-check is captured by ``lstat`` at the top of this
    call (i.e. immediately after the parent's scan produced this entry),
    narrowing the TOCTOU window rather than eliminating it -- there is no
    kernel-pinned handle to compare against the way STRICT tier's
    ``directory_fd`` provides.
    """

    component = _runtime_component(name)
    if depth > MAX_TRANSACTION_RUNTIME_TREE_DEPTH:
        raise TransactionRecoveryError(
            "CORRUPT_RUNTIME_STATE", "transaction runtime tree is nested too deeply"
        )
    if remaining is None:
        remaining = [MAX_TRANSACTION_RUNTIME_TREE_ENTRIES]
    is_path_mode = isinstance(directory, Path)
    if is_path_mode:
        try:
            entry_identity = directory.lstat()
        except FileNotFoundError as exc:
            raise _LockIdentityChanged(
                f"runtime directory changed before removal: {component}"
            ) from exc
    child_names = _bounded_runtime_names(
        directory,
        limit=remaining[0],
        label=f"transaction runtime {component}",
    )
    remaining[0] -= len(child_names)
    for child_name in child_names:
        if is_path_mode:
            metadata = (directory / child_name).lstat()
        else:
            metadata = os.stat(child_name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            if depth >= MAX_TRANSACTION_RUNTIME_TREE_DEPTH:
                raise TransactionRecoveryError(
                    "CORRUPT_RUNTIME_STATE",
                    "transaction runtime contains an unsupported nested directory",
                )
            child_ref = _open_runtime_directory_at(
                directory, child_name, create=False
            )
            try:
                if not is_path_mode and not _lock_entry_matches(
                    directory, child_name, child_ref
                ):
                    raise _LockIdentityChanged(
                        f"runtime directory changed before removal: {child_name}"
                    )
                _remove_pinned_runtime_tree_at(
                    directory,
                    child_name,
                    child_ref,
                    remaining=remaining,
                    depth=depth + 1,
                )
            finally:
                if not is_path_mode:
                    os.close(child_ref)
        else:
            if is_path_mode:
                (directory / child_name).unlink()
            else:
                os.unlink(child_name, dir_fd=directory)
    if is_path_mode:
        try:
            current = directory.lstat()
        except FileNotFoundError as exc:
            raise _LockIdentityChanged(
                f"runtime directory changed before removal: {component}"
            ) from exc
        if not is_same_object(current, entry_identity):
            raise _LockIdentityChanged(
                f"runtime directory changed before removal: {component}"
            )
        directory.rmdir()
        return
    if not _lock_entry_matches(parent, component, directory):
        raise _LockIdentityChanged(
            f"runtime directory changed before removal: {component}"
        )
    os.rmdir(component, dir_fd=parent)
    try:
        os.fsync(parent)
    except OSError:
        pass


@dataclass
class _OperationStore:
    name: str
    parent_fd: int | Path
    fd: int | Path
    backups_fd: int | Path | None = None

    def open_backups(self, *, create: bool) -> int | Path | None:
        if self.backups_fd is not None:
            return self.backups_fd
        try:
            self.backups_fd = _open_runtime_directory_at(
                self.fd, "backups", create=create
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TransactionRecoveryError(
                "CORRUPT_RUNTIME_STATE", f"cannot open transaction backups: {exc}"
            ) from exc
        return self.backups_fd

    def exists(self, name: str) -> bool:
        return _runtime_entry_metadata(self.fd, name) is not None

    def read_json(self, name: str, *, label: str) -> Any:
        return _read_runtime_json_at(self.fd, name, label=label)

    def write_json(
        self,
        name: str,
        value: Any,
        *,
        error_type: type[TransactionError] = TransactionRecoveryError,
    ) -> None:
        _atomic_runtime_json_at(self.fd, name, value, error_type=error_type)

    def write_bundle(self, value: Mapping[str, Any]) -> None:
        data = _json_bytes(dict(value))
        if len(data) > MAX_TRANSACTION_BUNDLE_BYTES:
            raise TransactionValidationError(
                "INVALID_BUNDLE",
                f"bundle exceeds the {MAX_TRANSACTION_BUNDLE_BYTES}-byte limit",
            )
        try:
            _atomic_runtime_write_at(self.fd, "bundle.json", data)
        except OSError as exc:
            raise TransactionRecoveryError(
                "CORRUPT_RUNTIME_STATE", f"cannot write confined bundle copy: {exc}"
            ) from exc

    def assert_current(self) -> None:
        if isinstance(self.fd, Path):
            # COMPATIBLE tier: point-in-time lstat, not pinned-descriptor
            # identity -- same narrowed-TOCTOU tradeoff as the rest of this
            # tier (see _remove_pinned_runtime_tree_at's docstring).
            try:
                current = self.fd.lstat()
            except OSError as exc:
                raise TransactionError(
                    "RUNTIME_NAMESPACE_CHANGED",
                    f"transaction runtime changed while held: {self.name}",
                ) from exc
            if is_name_surrogate(current) or not stat.S_ISDIR(current.st_mode):
                raise TransactionError(
                    "RUNTIME_NAMESPACE_CHANGED",
                    f"transaction runtime changed while held: {self.name}",
                )
            if self.backups_fd is not None:
                assert isinstance(self.backups_fd, Path)
                try:
                    backups_current = self.backups_fd.lstat()
                except OSError as exc:
                    raise TransactionError(
                        "RUNTIME_NAMESPACE_CHANGED",
                        f"transaction backups changed while held: {self.name}",
                    ) from exc
                if is_name_surrogate(backups_current) or not stat.S_ISDIR(
                    backups_current.st_mode
                ):
                    raise TransactionError(
                        "RUNTIME_NAMESPACE_CHANGED",
                        f"transaction backups changed while held: {self.name}",
                    )
            return
        if not _lock_entry_matches(self.parent_fd, self.name, self.fd):
            raise TransactionError(
                "RUNTIME_NAMESPACE_CHANGED",
                f"transaction runtime changed while held: {self.name}",
            )
        if self.backups_fd is not None and not _lock_entry_matches(
            self.fd, "backups", self.backups_fd
        ):
            raise TransactionError(
                "RUNTIME_NAMESPACE_CHANGED",
                f"transaction backups changed while held: {self.name}",
            )

    def close(self) -> None:
        if isinstance(self.fd, Path):
            self.backups_fd = None
            return
        if self.backups_fd is not None:
            os.close(self.backups_fd)
            self.backups_fd = None
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "_OperationStore":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


@dataclass
class _RuntimeStore:
    root_fd: int | Path
    meta_fd: int | Path
    transactions_fd: int | Path | None

    @classmethod
    def from_lock(cls, lock: MutationLock, *, create: bool) -> "_RuntimeStore":
        if lock._win_root_handle is not None:
            return cls._from_lock_compatible(lock, create=create)
        root_fd = lock.duplicate_root_fd()
        meta_fd = lock.duplicate_parent_fd()
        transactions_fd: int | None = None
        try:
            try:
                transactions_fd = _open_runtime_directory_at(
                    meta_fd, "transactions", create=create
                )
            except FileNotFoundError:
                if create:
                    raise
            except OSError as exc:
                raise TransactionRecoveryError(
                    "CORRUPT_RUNTIME_STATE", f"cannot open transactions runtime: {exc}"
                ) from exc
            return cls(root_fd, meta_fd, transactions_fd)
        except BaseException:
            if transactions_fd is not None:
                os.close(transactions_fd)
            os.close(meta_fd)
            os.close(root_fd)
            raise

    @classmethod
    def _from_lock_compatible(cls, lock: MutationLock, *, create: bool) -> "_RuntimeStore":
        """COMPATIBLE tier (native Windows): MutationLock never sets a
        POSIX fd there (see ``_acquire_compatible``), so this builds the
        store from plain paths instead of ``duplicate_root_fd``/
        ``duplicate_parent_fd``, which only work in STRICT tier."""

        if not lock.acquired:
            raise TransactionError(
                "LOCK_NOT_HELD", "vault root is unavailable outside a held mutation lock"
            )
        root = lock.vault_root
        meta = root / ".vault-meta"
        transactions: Path | None = None
        try:
            transactions = _open_runtime_directory_at(meta, "transactions", create=create)
        except FileNotFoundError:
            if create:
                raise
        except OSError as exc:
            raise TransactionRecoveryError(
                "CORRUPT_RUNTIME_STATE", f"cannot open transactions runtime: {exc}"
            ) from exc
        return cls(root, meta, transactions)

    def operation_names(self) -> list[str]:
        if self.transactions_fd is None:
            return []
        return _bounded_runtime_names(
            self.transactions_fd,
            limit=MAX_TRANSACTION_RUNTIME_ENTRIES,
            label="transactions runtime",
        )

    def assert_current(self) -> None:
        if isinstance(self.meta_fd, Path):
            if self.transactions_fd is not None:
                assert isinstance(self.transactions_fd, Path)
                try:
                    current = self.transactions_fd.lstat()
                except OSError as exc:
                    raise TransactionError(
                        "RUNTIME_NAMESPACE_CHANGED",
                        "transactions runtime changed while the mutation lock was held",
                    ) from exc
                if is_name_surrogate(current) or not stat.S_ISDIR(current.st_mode):
                    raise TransactionError(
                        "RUNTIME_NAMESPACE_CHANGED",
                        "transactions runtime changed while the mutation lock was held",
                    )
            return
        if self.transactions_fd is not None and not _lock_entry_matches(
            self.meta_fd, "transactions", self.transactions_fd
        ):
            raise TransactionError(
                "RUNTIME_NAMESPACE_CHANGED",
                "transactions runtime changed while the mutation lock was held",
            )

    def open_operation(self, name: str, *, create: bool) -> _OperationStore | None:
        operation_id = safe_operation_id(name)
        if self.transactions_fd is None:
            if not create:
                return None
            raise TransactionRecoveryError(
                "CORRUPT_RUNTIME_STATE", "transactions runtime is unavailable"
            )
        try:
            descriptor = _open_runtime_directory_at(
                self.transactions_fd, operation_id, create=create
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TransactionRecoveryError(
                "CORRUPT_RUNTIME_STATE",
                f"cannot open transaction {operation_id}: {exc}",
            ) from exc
        return _OperationStore(operation_id, self.transactions_fd, descriptor)

    def remove_operation(self, operation: _OperationStore) -> None:
        if self.transactions_fd is None:
            raise TransactionRecoveryError(
                "CORRUPT_RUNTIME_STATE", "transactions runtime is unavailable"
            )
        try:
            _remove_pinned_runtime_tree_at(
                self.transactions_fd, operation.name, operation.fd
            )
        except (OSError, _LockIdentityChanged) as exc:
            raise TransactionRecoveryError(
                "CORRUPT_RUNTIME_STATE",
                f"cannot remove transaction {operation.name}: {exc}",
            ) from exc

    def close(self) -> None:
        if isinstance(self.meta_fd, Path):
            self.transactions_fd = None
            return
        if self.transactions_fd is not None:
            os.close(self.transactions_fd)
            self.transactions_fd = None
        os.close(self.meta_fd)
        os.close(self.root_fd)

    def __enter__(self) -> "_RuntimeStore":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


@dataclass(frozen=True)
class PreparedWrite:
    relative_path: str
    target: Path
    mode: str
    content: bytes
    content_sha256: str
    original_sha256: str | None
    original_mode: int | None
    new_mode: int
    backup_path: Path


@dataclass(frozen=True)
class RecoveryWrite:
    relative_path: str
    original_sha256: str | None
    new_sha256: str
    original_mode: int | None
    new_mode: int
    backup_content: bytes | None


def _load_bundle(
    bundle_or_path: Mapping[str, Any] | Path | str,
) -> tuple[dict[str, Any], Path]:
    if isinstance(bundle_or_path, Mapping):
        try:
            encoded = _json_bytes(dict(bundle_or_path))
            snapshot = _strict_json_loads(encoded.decode("utf-8"))
        except (TypeError, ValueError, RuntimeError) as exc:
            raise TransactionValidationError(
                "INVALID_BUNDLE", f"cannot serialize bundle: {exc}"
            ) from exc
        if not isinstance(snapshot, dict):
            raise TransactionValidationError(
                "INVALID_BUNDLE", "bundle root must be a JSON object"
            )
        bundle = snapshot
        encoded_size = len(encoded)
        if encoded_size > MAX_TRANSACTION_BUNDLE_BYTES:
            raise TransactionValidationError(
                "INVALID_BUNDLE",
                f"bundle exceeds the {MAX_TRANSACTION_BUNDLE_BYTES}-byte limit",
            )
        return bundle, Path.cwd()
    path = Path(bundle_or_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    descriptor = -1
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("bundle path must be a no-follow regular file")
        if before.st_size > MAX_TRANSACTION_BUNDLE_BYTES:
            raise ValueError(
                f"bundle exceeds the {MAX_TRANSACTION_BUNDLE_BYTES}-byte limit"
            )
        descriptor = os.open(path, read_open_flags())
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > MAX_TRANSACTION_BUNDLE_BYTES
        ):
            raise ValueError("bundle file changed before it could be read")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(
                descriptor,
                min(1024 * 1024, MAX_TRANSACTION_BUNDLE_BYTES + 1 - total),
            )
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > MAX_TRANSACTION_BUNDLE_BYTES:
                raise ValueError(
                    f"bundle exceeds the {MAX_TRANSACTION_BUNDLE_BYTES}-byte limit"
                )
        after = os.fstat(descriptor)
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")
        if any(getattr(opened, field) != getattr(after, field) for field in stable):
            raise ValueError("bundle file changed while it was read")
        bundle = _strict_json_loads(b"".join(chunks).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TransactionValidationError(
            "INVALID_BUNDLE", f"cannot read bundle: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(bundle, dict):
        raise TransactionValidationError(
            "INVALID_BUNDLE", "bundle root must be an object"
        )
    if len(_json_bytes(bundle)) > MAX_TRANSACTION_BUNDLE_BYTES:
        raise TransactionValidationError(
            "INVALID_BUNDLE",
            "bundle exceeds the canonical serialized-size limit",
        )
    try:
        bundle_dir = path.parent.resolve(strict=True)
    except OSError as exc:
        raise TransactionValidationError(
            "INVALID_BUNDLE", f"cannot resolve bundle directory: {exc}"
        ) from exc
    return bundle, bundle_dir


def _raw_write_bytes(raw: Mapping[str, Any], bundle_dir: Path) -> bytes:
    has_inline = "content" in raw
    has_file = "content_file" in raw
    if has_inline == has_file:
        raise TransactionValidationError(
            "INVALID_WRITE_CONTENT",
            "write must contain exactly one of content or content_file",
        )
    if has_inline:
        value = raw["content"]
        if not isinstance(value, str):
            raise TransactionValidationError(
                "INVALID_WRITE_CONTENT", "inline write content must be a string"
            )
        content = value.encode("utf-8")
        if len(content) > MAX_TRANSACTION_FILE_BYTES:
            raise TransactionValidationError(
                "TRANSACTION_FILE_TOO_LARGE",
                f"inline write exceeds {MAX_TRANSACTION_FILE_BYTES} bytes",
            )
        return content
    supplied = raw["content_file"]
    if not isinstance(supplied, str) or not supplied or "\x00" in supplied:
        raise TransactionValidationError(
            "INVALID_WRITE_CONTENT", "content_file must be a non-empty path string"
        )
    source = Path(supplied)
    if not source.is_absolute():
        source = bundle_dir / source
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise TransactionValidationError(
            "CONTENT_FILE_MISSING", f"cannot inspect content file {source}: {exc}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise TransactionValidationError(
            "CONTENT_FILE_SYMLINK", f"content file may not be a symlink: {source}"
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise TransactionValidationError(
            "CONTENT_FILE_NOT_REGULAR", f"content file must be regular: {source}"
        )
    if metadata.st_size > MAX_TRANSACTION_FILE_BYTES:
        raise TransactionValidationError(
            "TRANSACTION_FILE_TOO_LARGE",
            f"content file exceeds {MAX_TRANSACTION_FILE_BYTES} bytes: {source}",
        )
    flags = read_open_flags()
    descriptor = -1
    try:
        descriptor = os.open(source, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino)
            or before.st_size > MAX_TRANSACTION_FILE_BYTES
        ):
            raise TransactionValidationError(
                "CONTENT_FILE_CHANGED", f"content file changed before read: {source}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(
                descriptor,
                min(1024 * 1024, MAX_TRANSACTION_FILE_BYTES + 1 - total),
            )
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > MAX_TRANSACTION_FILE_BYTES:
                raise TransactionValidationError(
                    "TRANSACTION_FILE_TOO_LARGE",
                    f"content file exceeds {MAX_TRANSACTION_FILE_BYTES} bytes: {source}",
                )
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")
        if any(
            getattr(before, field) != getattr(after, field) for field in stable_fields
        ):
            raise TransactionValidationError(
                "CONTENT_FILE_CHANGED", f"content file changed during read: {source}"
            )
        return b"".join(chunks)
    except OSError as exc:
        raise TransactionValidationError(
            "CONTENT_FILE_MISSING", f"cannot read content file {source}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _inject_address(markdown: str, address: str, relative_path: str) -> str:
    lines = markdown.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise TransactionValidationError(
            "ADDRESS_FRONTMATTER_REQUIRED",
            f"addressed page has no YAML frontmatter: {relative_path}",
        )
    closing_index = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        ),
        None,
    )
    if closing_index is None:
        raise TransactionValidationError(
            "ADDRESS_FRONTMATTER_UNTERMINATED",
            f"addressed page has unterminated YAML frontmatter: {relative_path}",
        )
    declared = [
        line.split(":", 1)[1].strip()
        for line in lines[1:closing_index]
        if line.startswith("address:")
    ]
    if len(declared) > 1:
        raise TransactionValidationError(
            "DUPLICATE_ADDRESS",
            f"addressed page declares address more than once: {relative_path}",
        )
    if declared:
        existing = declared[0]
        if existing != address:
            raise TransactionConflict(
                "ADDRESS_MISMATCH",
                f"{relative_path} already declares {existing}, expected {address}",
            )
        return markdown
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    lines.insert(1, f"address: {address}{newline}")
    return "".join(lines)


_CANONICAL_ADDRESS = re.compile(r"^[cl]-[0-9]{6}$")


def _validate_address_map(vault_root: Path, addresses: Mapping[Any, Any]) -> set[str]:
    """Reject corrupt address ledgers before they can influence page content."""

    seen: dict[str, str] = {}
    for relative, address in addresses.items():
        if not isinstance(relative, str):
            raise TransactionValidationError(
                "INVALID_ADDRESS_MAP", "address map keys must be strings"
            )
        try:
            normalized = _normalize_vault_path(relative)
        except TransactionValidationError as exc:
            raise TransactionValidationError(
                "INVALID_ADDRESS_MAP", f"unsafe address map page {relative!r}: {exc}"
            ) from exc
        parsed = PurePosixPath(normalized)
        if (
            not parsed.parts
            or parsed.parts[0] != "wiki"
            or parsed.suffix.lower() != ".md"
        ):
            raise TransactionValidationError(
                "INVALID_ADDRESS_MAP",
                f"address map keys must be canonical wiki Markdown paths: {relative!r}",
            )
        if (
            not isinstance(address, str)
            or _CANONICAL_ADDRESS.fullmatch(address) is None
            or int(address[2:]) < 1
        ):
            raise TransactionValidationError(
                "INVALID_ADDRESS_MAP",
                f"address map value for {relative} must match c-000001 or l-000001",
            )
        prior = seen.get(address)
        if prior is not None:
            raise TransactionValidationError(
                "INVALID_ADDRESS_MAP",
                f"address {address} is assigned to both {prior} and {relative}",
            )
        seen[address] = relative
    return set(seen)


def _expand_managed_metadata(
    vault_root: Path,
    bundle: dict[str, Any],
    bundle_dir: Path,
    *,
    root_fd: int | Path | None = None,
    meta_fd: int | Path | None = None,
) -> dict[str, Any]:
    """Expand address/source requests into ordinary transaction writes.

    Expansion happens while the vault-wide mutation lock is held, so counter,
    page frontmatter, address map, and source records share one journal.
    """

    address_requests = bundle.get("address_requests", [])
    source_updates = bundle.get("source_manifest_updates", {})
    if not isinstance(address_requests, list):
        raise TransactionValidationError(
            "INVALID_ADDRESS_REQUESTS", "address_requests must be an array"
        )
    if not isinstance(source_updates, dict):
        raise TransactionValidationError(
            "INVALID_SOURCE_UPDATES", "source_manifest_updates must be an object"
        )
    operation_type = bundle.get("operation_type")
    raw_writes = bundle.get("writes")
    if not isinstance(raw_writes, list):
        raise TransactionValidationError("NO_WRITES", "bundle writes must be an array")
    managed_by_key = {
        _portable_name_key(path): path for path in _MANAGED_METADATA_PATHS
    }
    direct_managed: set[str] = set()
    for raw in raw_writes:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            continue
        supplied = raw["path"]
        canonical_managed = managed_by_key.get(_portable_name_key(supplied))
        if canonical_managed is not None:
            if supplied != canonical_managed:
                raise TransactionValidationError(
                    "CASEFOLD_PATH_ALIAS",
                    f"managed metadata path must use canonical case: {supplied}",
                )
            direct_managed.add(supplied)
    unauthorized_managed = {
        path
        for path in direct_managed
        if operation_type not in _DIRECT_MANAGED_METADATA_AUTHORITY[path]
    }
    if unauthorized_managed:
        raise TransactionValidationError(
            "MANAGED_METADATA_COLLISION",
            "managed metadata is request-owned for this operation type: "
            + ", ".join(sorted(unauthorized_managed)),
        )
    if (
        address_requests or source_updates
    ) and operation_type not in _MANAGED_REQUEST_OPERATIONS:
        raise TransactionValidationError(
            "MANAGED_REQUEST_SCOPE_VIOLATION",
            "only ingest and autoresearch operations may submit managed metadata requests",
        )
    if not address_requests and not source_updates:
        return bundle

    expanded_value = json.loads(json.dumps(bundle))
    if not isinstance(expanded_value, dict):  # defensive type narrowing
        raise TransactionValidationError(
            "INVALID_BUNDLE", "bundle root must be an object"
        )
    expanded: dict[str, Any] = expanded_value
    writes = expanded.get("writes")
    if not isinstance(writes, list):
        raise TransactionValidationError("NO_WRITES", "bundle writes must be an array")
    by_path: dict[str, dict[str, Any]] = {}
    for raw in writes:
        if isinstance(raw, dict) and isinstance(raw.get("path"), str):
            by_path[raw["path"]] = raw
    collision = {
        path
        for path in by_path
        if _portable_name_key(path)
        in {_portable_name_key(managed) for managed in _MANAGED_METADATA_PATHS}
    }
    if collision:
        raise TransactionValidationError(
            "MANAGED_METADATA_COLLISION",
            "managed metadata must be expressed through address/source requests: "
            + ", ".join(sorted(collision)),
        )

    manifest_bytes = read_vault_regular(
        vault_root,
        ".raw/.manifest.json",
        limit=16 * 1024 * 1024,
        root_fd=root_fd,
        meta_fd=meta_fd,
    )
    if manifest_bytes is not None:
        try:
            manifest = _strict_json_loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise TransactionValidationError(
                "INVALID_SOURCE_MANIFEST",
                f"cannot read existing source manifest: {exc}",
            ) from exc
        if not isinstance(manifest, dict):
            raise TransactionValidationError(
                "INVALID_SOURCE_MANIFEST", "source manifest root must be an object"
            )
        manifest_mode = "replace"
    else:
        manifest = {"version": 1, "sources": {}, "address_map": {}}
        manifest_mode = "create"
    sources = manifest.setdefault("sources", {})
    addresses = manifest.setdefault("address_map", {})
    if not isinstance(sources, dict) or not isinstance(addresses, dict):
        raise TransactionValidationError(
            "INVALID_SOURCE_MANIFEST", "sources and address_map must be objects"
        )
    used_addresses = _validate_address_map(vault_root, addresses)
    for source_id, record in source_updates.items():
        if (
            not isinstance(source_id, str)
            or not source_id
            or not isinstance(record, dict)
        ):
            raise TransactionValidationError(
                "INVALID_SOURCE_UPDATE",
                "source updates require non-empty string keys and objects",
            )
        sources[source_id] = record

    if address_requests:
        counter_bytes = read_vault_regular(
            vault_root,
            ".vault-meta/address-counter.txt",
            limit=64 * 1024,
            root_fd=root_fd,
            meta_fd=meta_fd,
        )
        if counter_bytes is not None:
            try:
                counter_text = counter_bytes.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise TransactionValidationError(
                    "INVALID_ADDRESS_COUNTER", f"cannot read address counter: {exc}"
                ) from exc
            if not counter_text.isascii() or not counter_text.isdigit():
                raise TransactionValidationError(
                    "INVALID_ADDRESS_COUNTER",
                    "address counter must contain decimal digits",
                )
            next_address = int(counter_text)
            counter_mode = "replace"
        else:
            next_address = 1
            counter_mode = "create"
        if next_address < 1:
            raise TransactionValidationError(
                "INVALID_ADDRESS_COUNTER", "address counter must be a positive integer"
            )
        if next_address > 999999:
            raise TransactionValidationError(
                "ADDRESS_SPACE_EXHAUSTED", "six-digit address space is exhausted"
            )
        seen_requests: set[str] = set()
        for request in address_requests:
            if not isinstance(request, dict):
                raise TransactionValidationError(
                    "INVALID_ADDRESS_REQUEST", "each address request must be an object"
                )
            relative = request.get("path")
            prefix = request.get("prefix", "c")
            if not isinstance(relative, str) or relative not in by_path:
                raise TransactionValidationError(
                    "INVALID_ADDRESS_REQUEST",
                    "address request path must name a bundle write",
                )
            if relative in seen_requests:
                raise TransactionValidationError(
                    "DUPLICATE_ADDRESS_REQUEST",
                    f"duplicate address request: {relative}",
                )
            seen_requests.add(relative)
            if prefix not in {"c", "l"}:
                raise TransactionValidationError(
                    "INVALID_ADDRESS_PREFIX", f"unsupported address prefix: {prefix!r}"
                )
            address = addresses.get(relative)
            if address is None:
                if next_address > 999999:
                    raise TransactionValidationError(
                        "ADDRESS_SPACE_EXHAUSTED",
                        "six-digit address space is exhausted",
                    )
                address = f"{prefix}-{next_address:06d}"
                if address in used_addresses:
                    raise TransactionValidationError(
                        "ADDRESS_COUNTER_COLLISION",
                        f"address counter would reassign existing address {address}",
                    )
                next_address += 1
                addresses[relative] = address
                used_addresses.add(address)
            elif not isinstance(address, str):
                raise TransactionValidationError(
                    "INVALID_ADDRESS_MAP",
                    f"address map value for {relative} must be a string",
                )
            elif not address.startswith(f"{prefix}-"):
                raise TransactionValidationError(
                    "ADDRESS_PREFIX_MISMATCH",
                    f"{relative} is mapped to {address}, not requested prefix {prefix}",
                )
            raw = by_path[relative]
            try:
                markdown = _raw_write_bytes(raw, bundle_dir).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise TransactionValidationError(
                    "ADDRESS_CONTENT_NOT_TEXT",
                    f"addressed page is not UTF-8 Markdown: {relative}",
                ) from exc
            raw.pop("content_file", None)
            raw["content"] = _inject_address(markdown, address, relative)
            raw.pop("sha256", None)
        writes.append(
            {
                "path": ".vault-meta/address-counter.txt",
                "mode": counter_mode,
                "content": f"{next_address}\n",
            }
        )
        expanded.setdefault("expected_hashes", {})[
            ".vault-meta/address-counter.txt"
        ] = _safe_hash(
            vault_root,
            ".vault-meta/address-counter.txt",
            root_fd=root_fd,
            meta_fd=meta_fd,
        )

    writes.append(
        {
            "path": ".raw/.manifest.json",
            "mode": manifest_mode,
            "content": _json_bytes(manifest).decode("utf-8"),
        }
    )
    expanded.setdefault("expected_hashes", {})[".raw/.manifest.json"] = _safe_hash(
        vault_root, ".raw/.manifest.json", root_fd=root_fd, meta_fd=meta_fd
    )
    return expanded


def _validate_markdown(data: bytes, relative_path: str) -> None:
    if not relative_path.lower().endswith(".md"):
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TransactionValidationError(
            "INVALID_MARKDOWN", f"{relative_path} is not UTF-8"
        ) from exc
    if text.startswith("---\n") and "\n---\n" not in text[4:]:
        raise TransactionValidationError(
            "INVALID_MARKDOWN", f"{relative_path} has unterminated frontmatter"
        )


def _validate_json(data: bytes, relative_path: str) -> None:
    if not relative_path.lower().endswith(".json"):
        return
    try:
        _strict_json_loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TransactionValidationError(
            "INVALID_JSON", f"{relative_path} is not valid JSON: {exc}"
        ) from exc


def _is_reserved_write_path(relative: str) -> bool:
    relative = _portable_name_key(relative)
    if any(
        relative == _portable_name_key(reserved)
        or relative.startswith(_portable_name_key(reserved) + "/")
        for reserved in _RESERVED_WRITE_PATHS
    ):
        return True
    return any(
        relative == _portable_name_key(prefix)
        or relative.startswith(_portable_name_key(prefix) + ".")
        or relative.startswith(_portable_name_key(prefix) + "/")
        for prefix in _RESERVED_WRITE_PREFIXES
    )


def _validate_operation_write_scope(
    operation_type: str,
    relative: str,
    *,
    write_mode: Any,
) -> None:
    """Enforce the declared workflow's writable content domain."""

    parts = PurePosixPath(relative).parts
    first = parts[0] if parts else ""
    required_case = _POLICY_ROOT_CASE.get(_portable_name_key(first))
    if required_case is not None and first != required_case:
        raise TransactionValidationError(
            "CASEFOLD_PATH_ALIAS",
            f"policy namespace must use canonical case {required_case!r}: {relative}",
        )
    if _is_reserved_write_path(relative):
        raise TransactionValidationError(
            "RESERVED_WRITE_PATH",
            f"product and runtime internals cannot be bundle targets: {relative}",
        )
    managed_by_key = {
        _portable_name_key(path): path for path in _MANAGED_METADATA_PATHS
    }
    managed = managed_by_key.get(_portable_name_key(relative))
    if managed is not None:
        if relative != managed:
            raise TransactionValidationError(
                "CASEFOLD_PATH_ALIAS",
                f"managed metadata path must use canonical case: {relative}",
            )
        if (
            operation_type in _MANAGED_REQUEST_OPERATIONS
            or operation_type in (_DIRECT_MANAGED_METADATA_AUTHORITY[managed])
        ):
            return
        raise TransactionValidationError(
            "WRITE_SCOPE_VIOLATION",
            f"{operation_type} operations may not write managed metadata: {relative}",
        )
    if operation_type in {"setup", "migration"}:
        allowed = _BOOTSTRAP_COMMON_PATHS | (
            _SETUP_EXTENSION_PATHS if operation_type == "setup" else set()
        )
        if relative not in allowed:
            raise TransactionValidationError(
                "WRITE_SCOPE_VIOLATION",
                f"{operation_type} operations may write only declared bootstrap paths: {relative}",
            )
    if operation_type == "configuration" and relative != ".vault-meta/mode.json":
        raise TransactionValidationError(
            "WRITE_SCOPE_VIOLATION",
            f"configuration operations may write only .vault-meta/mode.json: {relative}",
        )
    if operation_type == "base" and not (
        relative.startswith("wiki/") and relative.endswith(".base")
    ):
        raise TransactionValidationError(
            "WRITE_SCOPE_VIOLATION",
            f"base operations may write only wiki .base files: {relative}",
        )
    if operation_type == "canvas" and not (
        (relative.startswith("wiki/canvases/") and relative.endswith(".canvas"))
        or relative == "wiki/canvases/index.md"
    ):
        raise TransactionValidationError(
            "WRITE_SCOPE_VIOLATION",
            "canvas operations may write only wiki/canvases/*.canvas "
            f"and wiki/canvases/index.md: {relative}",
        )
    if operation_type == "fold" and not (
        (
            relative.startswith("wiki/folds/")
            and relative.endswith(".md")
            and len(PurePosixPath(relative).parts) == 3
        )
        or relative in {"wiki/index.md", "wiki/log.md"}
    ):
        raise TransactionValidationError(
            "WRITE_SCOPE_VIOLATION",
            f"fold operations may write only one fold page, wiki/index.md, and wiki/log.md: {relative}",
        )
    if operation_type in _WIKI_ONLY_OPERATIONS and not relative.startswith("wiki/"):
        raise TransactionValidationError(
            "WRITE_SCOPE_VIOLATION",
            f"{operation_type} operations may write only wiki content: {relative}",
        )
    if operation_type in _WIKI_AND_RAW_OPERATIONS and not (
        relative.startswith("wiki/") or relative.startswith(".raw/")
    ):
        raise TransactionValidationError(
            "WRITE_SCOPE_VIOLATION",
            f"{operation_type} operations may write only wiki or raw-source content: {relative}",
        )
    if operation_type == "capture":
        parts = PurePosixPath(relative).parts
        if (
            write_mode != "create"
            or len(parts) != 3
            or parts[:2] != (".raw", "captured")
        ):
            raise TransactionValidationError(
                "WRITE_SCOPE_VIOLATION",
                f"capture operations may only create content-addressed captured files: {relative}",
            )


def _validate_operation_bundle_scope(
    operation_type: str,
    prepared: Iterable[PreparedWrite],
) -> None:
    """Validate coupled path sets that cannot be checked one write at a time."""

    if operation_type != "fold":
        return
    paths = {write.relative_path for write in prepared}
    fold_pages = {
        path
        for path in paths
        if path.startswith("wiki/folds/") and path.endswith(".md")
    }
    if len(fold_pages) != 1 or paths != fold_pages | {"wiki/index.md", "wiki/log.md"}:
        raise TransactionValidationError(
            "WRITE_SCOPE_VIOLATION",
            "fold operations require exactly one fold page plus wiki/index.md and wiki/log.md",
        )


def _prepare_writes(
    vault_root: Path,
    bundle: Mapping[str, Any],
    bundle_dir: Path,
    transaction_dir: Path | None,
    *,
    backups_fd: int | Path | None = None,
    root_fd: int | Path | None = None,
    meta_fd: int | Path | None = None,
) -> list[PreparedWrite]:
    raw_writes = bundle.get("writes")
    if not isinstance(raw_writes, list) or not raw_writes:
        raise TransactionValidationError(
            "NO_WRITES", "bundle must contain at least one write"
        )
    if len(raw_writes) > MAX_TRANSACTION_WRITES:
        raise TransactionValidationError(
            "TRANSACTION_WRITE_LIMIT",
            f"bundle exceeds the {MAX_TRANSACTION_WRITES}-write recovery limit",
        )
    # Lexical portability pre-pass before any filesystem probe so unportable
    # plans are rejected with the same code on every platform (a Windows lstat
    # of e.g. "a:b.md" would otherwise fail first with a different error).
    # expected_hashes keys are deliberately exempt: probe-only keys are read
    # preconditions, and a legacy vault must stay able to pin the state of a
    # pre-existing file whose historical name violates the portable rules.
    for raw in raw_writes:
        if isinstance(raw, dict) and isinstance(raw.get("path"), str):
            _assert_portable_write_path(raw["path"])
    expected = bundle.get("expected_hashes")
    if not isinstance(expected, dict):
        raise TransactionValidationError(
            "INVALID_EXPECTED_HASHES", "expected_hashes must be an object"
        )
    if len(expected) > MAX_TRANSACTION_WRITES:
        raise TransactionValidationError(
            "TRANSACTION_WRITE_LIMIT",
            f"expected hashes exceed the {MAX_TRANSACTION_WRITES}-path recovery limit",
        )
    normalized_expected: dict[str, str | None] = {}
    expected_casefold: dict[str, str] = {}
    for raw_path, digest in expected.items():
        normalized_path = (
            _normalize_vault_path(raw_path)
            if isinstance(root_fd, int)
            else _safe_vault_path(vault_root, raw_path)[0]
        )
        _assert_no_existing_portable_alias(
            vault_root,
            normalized_path,
            root_fd=root_fd,
            meta_fd=meta_fd,
        )
        folded_path = _portable_name_key(normalized_path)
        prior_expected = expected_casefold.get(folded_path)
        if prior_expected is not None and prior_expected != normalized_path:
            raise TransactionValidationError(
                "CASEFOLD_PATH_COLLISION",
                f"expected hashes contain case-colliding paths: {prior_expected}, {normalized_path}",
            )
        expected_casefold[folded_path] = normalized_path
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != 64
            or digest != digest.lower()
            or any(
                character not in "0123456789abcdef" for character in digest.casefold()
            )
        ):
            raise TransactionValidationError(
                "INVALID_EXPECTED_HASH",
                f"expected hash for {normalized_path} must be SHA-256 or null",
            )
        normalized_expected[normalized_path] = digest
    seen: set[str] = set()
    seen_casefold: dict[str, str] = {}
    prepared: list[PreparedWrite] = []
    total_content_bytes = 0
    total_backup_bytes = 0
    if backups_fd is None:
        if transaction_dir is None:
            raise TransactionValidationError(
                "UNSAFE_RUNTIME_PATH", "transaction backup destination is unavailable"
            )
        backup_dir = transaction_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(backup_dir, 0o700)
    else:
        backup_dir = Path("backups")

    for index, raw in enumerate(raw_writes):
        if not isinstance(raw, dict):
            raise TransactionValidationError(
                "INVALID_WRITE", f"write {index} must be an object"
            )
        relative = raw.get("path")
        if not isinstance(root_fd, int):
            normalized, target = _safe_vault_path(vault_root, relative)
        else:
            normalized = _normalize_vault_path(relative)
            target = vault_root.joinpath(*PurePosixPath(normalized).parts)
        _assert_no_existing_portable_alias(
            vault_root,
            normalized,
            root_fd=root_fd,
            meta_fd=meta_fd,
        )
        if normalized in seen:
            raise TransactionValidationError(
                "DUPLICATE_WRITE", f"duplicate write path: {normalized}"
            )
        seen.add(normalized)
        folded_path = _portable_name_key(normalized)
        prior_write = seen_casefold.get(folded_path)
        if prior_write is not None and prior_write != normalized:
            raise TransactionValidationError(
                "CASEFOLD_PATH_COLLISION",
                f"bundle contains case-colliding writes: {prior_write}, {normalized}",
            )
        seen_casefold[folded_path] = normalized
        if normalized not in normalized_expected:
            raise TransactionValidationError(
                "MISSING_EXPECTED_HASH",
                f"every write requires an expected_hashes entry: {normalized}",
            )
        mode = raw.get("mode")
        if mode not in {"create", "replace"}:
            raise TransactionValidationError(
                "INVALID_WRITE_MODE", f"{relative} mode must be create or replace"
            )
        _validate_operation_write_scope(
            str(bundle.get("operation_type")),
            normalized,
            write_mode=mode,
        )
        if (
            _portable_name_key(normalized).startswith(".raw/")
            and _portable_name_key(normalized) != ".raw/.manifest.json"
            and mode != "create"
        ):
            raise TransactionValidationError(
                "RAW_IS_CREATE_ONLY",
                f"raw source payloads cannot be replaced: {relative}",
            )
        content = _raw_write_bytes(raw, bundle_dir)
        total_content_bytes += len(content)
        if total_content_bytes > MAX_TRANSACTION_TOTAL_BYTES:
            raise TransactionValidationError(
                "TRANSACTION_TOTAL_TOO_LARGE",
                f"transaction content exceeds {MAX_TRANSACTION_TOTAL_BYTES} bytes",
            )
        content_hash = sha256_bytes(content)
        declared_hash = raw.get("sha256")
        if "content_file" in raw and declared_hash is None:
            raise TransactionValidationError(
                "CONTENT_HASH_REQUIRED",
                f"content_file writes require a declared SHA-256: {relative}",
            )
        if declared_hash is not None and (
            not isinstance(declared_hash, str)
            or len(declared_hash) != 64
            or declared_hash != declared_hash.lower()
            or any(character not in "0123456789abcdef" for character in declared_hash)
        ):
            raise TransactionValidationError(
                "INVALID_CONTENT_HASH",
                f"declared hash must be lowercase SHA-256: {relative}",
            )
        if declared_hash is not None and declared_hash != content_hash:
            raise TransactionValidationError(
                "CONTENT_HASH_MISMATCH",
                f"declared hash does not match content for {relative}",
            )
        _validate_json(content, normalized)
        _validate_markdown(content, normalized)
        current_hash, original_mode = _safe_file_state(
            vault_root,
            normalized,
            max_bytes=MAX_TRANSACTION_FILE_BYTES,
            root_fd=root_fd,
            meta_fd=meta_fd,
        )
        if normalized_expected[normalized] != current_hash:
            raise TransactionConflict(
                "EXPECTED_HASH_MISMATCH",
                f"{normalized} changed since the operation was drafted",
            )
        if mode == "create" and current_hash is not None:
            raise TransactionConflict(
                "CREATE_TARGET_EXISTS", f"create target exists: {normalized}"
            )
        if mode == "replace" and current_hash is None:
            raise TransactionConflict(
                "REPLACE_TARGET_MISSING", f"replace target is missing: {normalized}"
            )
        backup = backup_dir / f"{index:04d}.original"
        if current_hash is not None:
            original_content = read_vault_regular(
                vault_root,
                normalized,
                limit=MAX_TRANSACTION_FILE_BYTES,
                missing_ok=False,
                root_fd=root_fd,
                meta_fd=meta_fd,
            )
            assert original_content is not None
            if sha256_bytes(original_content) != current_hash:
                raise TransactionConflict(
                    "EXPECTED_HASH_MISMATCH",
                    f"{normalized} changed while the operation was prepared",
                )
            total_backup_bytes += len(original_content)
            if total_backup_bytes > MAX_TRANSACTION_TOTAL_BYTES:
                raise TransactionValidationError(
                    "TRANSACTION_TOTAL_TOO_LARGE",
                    f"transaction backups exceed {MAX_TRANSACTION_TOTAL_BYTES} bytes",
                )
            if backups_fd is None:
                atomic_write(backup, original_content, mode=0o600)
            else:
                _atomic_runtime_write_at(
                    backups_fd, backup.name, original_content, mode=0o600
                )
        prepared.append(
            PreparedWrite(
                relative_path=normalized,
                target=target,
                mode=mode,
                content=content,
                content_sha256=content_hash,
                original_sha256=current_hash,
                original_mode=original_mode,
                new_mode=original_mode if original_mode is not None else 0o600,
                backup_path=backup,
            )
        )
    extra_expected = sorted(set(normalized_expected).difference(seen))
    if extra_expected:
        raise TransactionValidationError(
            "UNUSED_EXPECTED_HASH",
            "expected_hashes contains paths that are not writes: "
            + ", ".join(extra_expected),
        )
    _validate_operation_bundle_scope(str(bundle.get("operation_type")), prepared)
    _validate_provenance_writes(vault_root, prepared, root_fd=root_fd, meta_fd=meta_fd)
    return prepared


def _validate_provenance_writes(
    vault_root: Path,
    prepared: Iterable[PreparedWrite],
    *,
    root_fd: int | Path | None = None,
    meta_fd: int | Path | None = None,
) -> None:
    """Validate the complete prospective source/claim ledger pair."""

    from .ledgers import (
        CLAIM_PATH,
        SOURCE_PATH,
        strict_json_loads,
        validate_claim_ledger,
        validate_source_ledger,
    )

    planned = {write.relative_path: write.content for write in prepared}
    if SOURCE_PATH not in planned and CLAIM_PATH not in planned:
        return

    def document(relative: str) -> Any | None:
        if relative in planned:
            data = planned[relative]
        else:
            digest = _safe_hash(vault_root, relative, root_fd=root_fd, meta_fd=meta_fd)
            if digest is None:
                return None
            loaded = read_vault_regular(
                vault_root,
                relative,
                limit=16 * 1024 * 1024,
                missing_ok=False,
                root_fd=root_fd,
                meta_fd=meta_fd,
            )
            assert loaded is not None
            data = loaded
            if sha256_bytes(data) != digest:
                raise TransactionConflict(
                    "EXPECTED_HASH_MISMATCH",
                    f"{relative} changed during ledger validation",
                )
        try:
            return strict_json_loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise TransactionValidationError(
                "INVALID_PROVENANCE_LEDGER", f"{relative} is not valid JSON: {exc}"
            ) from exc

    source = document(SOURCE_PATH)
    claims = document(CLAIM_PATH)
    errors: list[dict[str, str]] = []

    def anchored_hash(relative: str) -> str | None:
        return _safe_hash(vault_root, relative, root_fd=root_fd, meta_fd=meta_fd)

    def anchored_read(relative: str) -> bytes | None:
        return read_vault_regular(
            vault_root,
            relative,
            limit=16 * 1024 * 1024,
            root_fd=root_fd,
            meta_fd=meta_fd,
        )

    if claims is not None and source is None:
        errors.append(
            {
                "path": SOURCE_PATH,
                "message": "source ledger is required when a claim ledger exists",
            }
        )
    if source is not None:
        if not isinstance(source, dict):
            errors.append(
                {"path": SOURCE_PATH, "message": "ledger root must be an object"}
            )
        else:
            try:
                errors.extend(
                    {
                        "path": f"{SOURCE_PATH}:{item['path']}",
                        "message": item["message"],
                    }
                    for item in validate_source_ledger(
                        source,
                        vault_root=vault_root,
                        planned_files={
                            path: sha256_bytes(content)
                            for path, content in planned.items()
                        },
                        hash_reader=anchored_hash,
                    )
                )
            except VaultSelectionError as exc:
                errors.append({"path": SOURCE_PATH, "message": str(exc)})
    if claims is not None:
        if not isinstance(claims, dict):
            errors.append(
                {"path": CLAIM_PATH, "message": "ledger root must be an object"}
            )
        else:
            source_for_claims = source if isinstance(source, dict) else {"sources": {}}
            errors.extend(
                {"path": f"{CLAIM_PATH}:{item['path']}", "message": item["message"]}
                for item in validate_claim_ledger(
                    claims,
                    source_for_claims,
                    vault_root=vault_root,
                    planned_files=planned,
                    file_reader=anchored_read,
                )
            )
    if errors:
        ordered = sorted(errors, key=lambda item: (item["path"], item["message"]))
        detail = "; ".join(f"{item['path']}: {item['message']}" for item in ordered)
        raise TransactionValidationError("INVALID_PROVENANCE_LEDGER", detail)


def _journal_for(
    operation_id: str,
    operation_type: str,
    writes: Iterable[PreparedWrite],
    *,
    input_bundle_hash: str,
    expanded_bundle_hash: str,
    approval_hash: str,
) -> dict[str, Any]:
    return {
        "schema": JOURNAL_SCHEMA,
        "operation_id": operation_id,
        "operation_type": operation_type,
        "input_bundle_sha256": input_bundle_hash,
        "expanded_bundle_sha256": expanded_bundle_hash,
        "approval_sha256": approval_hash,
        "state": "prepared",
        "created_epoch": time.time(),
        "writes": [
            {
                "path": write.relative_path,
                "mode": write.mode,
                "new_sha256": write.content_sha256,
                "original_sha256": write.original_sha256,
                "original_mode": write.original_mode,
                "new_mode": write.new_mode,
                "backup": write.backup_path.name,
            }
            for write in writes
        ],
        "applied": [],
    }


def _result_for(
    operation_id: str,
    operation_type: str,
    writes: Iterable[PreparedWrite],
    *,
    input_bundle_hash: str,
    expanded_bundle_hash: str,
    approval_hash: str,
) -> dict[str, Any]:
    prepared = list(writes)
    return {
        "schema": RESULT_SCHEMA,
        "operation_id": operation_id,
        "operation_type": operation_type,
        "bundle_sha256": input_bundle_hash,
        "expanded_bundle_sha256": expanded_bundle_hash,
        "approval_sha256": approval_hash,
        "status": "complete",
        "changed_paths": [write.relative_path for write in prepared],
        "hashes": {write.relative_path: write.content_sha256 for write in prepared},
        "modes": {write.relative_path: write.new_mode for write in prepared},
    }


def _validate_runtime_document_size(value: Any, *, label: str) -> None:
    size = len(_json_bytes(value))
    if size > MAX_TRANSACTION_RUNTIME_JSON_BYTES:
        raise TransactionValidationError(
            "TRANSACTION_RUNTIME_STATE_TOO_LARGE",
            f"{label} would be {size} bytes, above the "
            f"{MAX_TRANSACTION_RUNTIME_JSON_BYTES}-byte recovery limit",
        )


def _validate_journal_envelope(
    journal: Mapping[str, Any], writes: Iterable[PreparedWrite]
) -> None:
    """Prove every normal journal state remains inside recovery's reader cap."""

    prepared = list(writes)
    probe = json.loads(json.dumps(journal))
    probe["state"] = "rollback-failed"
    probe["applied"] = [write.relative_path for write in prepared]
    probe["completed_epoch"] = 9_999_999_999.999999
    probe["rolled_back_epoch"] = 9_999_999_999.999999
    probe["recovery_failures"] = [
        f"{write.relative_path}: " + ("x" * 1024) for write in prepared
    ]
    _validate_runtime_document_size(probe, label="transaction journal")


def _valid_journal_sha256(value: Any, *, nullable: bool = False) -> bool:
    if nullable and value is None:
        return True
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _read_recovery_backup_at(directory_fd: int | Path, name: str, *, label: str) -> bytes:
    try:
        return _read_runtime_bytes_at(
            directory_fd,
            name,
            label=label,
            limit=MAX_RECOVERY_BACKUP_BYTES,
        )
    except TransactionRecoveryError as exc:
        raise TransactionRecoveryError("CORRUPT_JOURNAL", str(exc)) from exc


def _validated_recovery_writes(
    vault_root: Path,
    transaction: _OperationStore,
    journal: Mapping[str, Any],
    *,
    root_fd: int | Path,
    meta_fd: int | Path,
) -> list[RecoveryWrite]:
    """Preflight the complete journal and all backups before rollback mutates."""

    writes = journal.get("writes")
    operation_type = journal.get("operation_type")
    if (
        not isinstance(writes, list)
        or not writes
        or operation_type not in OPERATION_TYPES
    ):
        raise TransactionRecoveryError(
            "CORRUPT_JOURNAL", "journal has invalid writes or operation type"
        )
    if len(writes) > MAX_TRANSACTION_WRITES:
        raise TransactionRecoveryError(
            "CORRUPT_JOURNAL", "journal exceeds the supported write-count limit"
        )
    needs_backups = any(
        isinstance(entry, dict) and entry.get("original_sha256") is not None
        for entry in writes
    )
    backup_metadata = _runtime_entry_metadata(transaction.fd, "backups")
    backup_fd = transaction.backups_fd
    if backup_fd is None and (needs_backups or backup_metadata is not None):
        if backup_metadata is None or not stat.S_ISDIR(backup_metadata.st_mode):
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL", "journal backup path is not a safe directory"
            )
        backup_fd = transaction.open_backups(create=False)
        if backup_fd is None:
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL", "journal backup directory is unavailable"
            )

    validated: list[RecoveryWrite] = []
    seen_casefold: dict[str, str] = {}
    total_backup_bytes = 0
    for index, entry in enumerate(writes):
        if not isinstance(entry, dict):
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL", f"journal write {index} is not an object"
            )
        expected_backup = f"{index:04d}.original"
        if entry.get("backup") != expected_backup:
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL",
                f"journal write {index} has an invalid backup binding",
            )
        mode = entry.get("mode")
        if mode not in {"create", "replace"}:
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL", f"journal write {index} has an invalid mode"
            )
        supplied_path = entry.get("path")
        if (
            not isinstance(supplied_path, str)
            or not supplied_path
            or any(
                character in supplied_path for character in ("\x00", "\n", "\r", "\\")
            )
        ):
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL", f"journal write {index} has an invalid path"
            )
        parsed_path = PurePosixPath(supplied_path)
        if (
            parsed_path.is_absolute()
            or supplied_path in {"", "."}
            or ".." in parsed_path.parts
            or parsed_path.as_posix() != supplied_path
        ):
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL", f"journal write {index} has an invalid path"
            )
        if (
            unicodedata.normalize("NFC", supplied_path) != supplied_path
            or len(supplied_path.encode("utf-8")) > MAX_TRANSACTION_PATH_BYTES
        ):
            # Deliberately NOT mirroring _assert_portable_write_path here:
            # recovery must be able to roll back a journal written by a release
            # that still accepted such paths.
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL",
                f"journal write {index} has a non-portable path",
            )
        relative = supplied_path
        try:
            _assert_no_existing_portable_alias(
                vault_root,
                relative,
                root_fd=root_fd,
                meta_fd=meta_fd,
            )
        except TransactionValidationError as exc:
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL",
                f"journal write {index} conflicts with a portable vault path alias",
            ) from exc
        try:
            _validate_operation_write_scope(
                str(operation_type), relative, write_mode=mode
            )
        except TransactionError as exc:
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL", f"journal write {index} has an unsafe path: {exc}"
            ) from exc
        folded = _portable_name_key(relative)
        prior = seen_casefold.get(folded)
        if prior is not None:
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL",
                f"journal has duplicate or case-colliding paths: {prior}, {relative}",
            )
        seen_casefold[folded] = relative
        original_hash = entry.get("original_sha256")
        new_hash = entry.get("new_sha256")
        if not _valid_journal_sha256(
            original_hash, nullable=True
        ) or not _valid_journal_sha256(new_hash):
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL", f"journal write {index} has an invalid hash"
            )
        assert original_hash is None or isinstance(original_hash, str)
        assert isinstance(new_hash, str)
        original_mode = entry.get("original_mode")
        new_mode = entry.get("new_mode")
        if (
            (original_hash is None and original_mode is not None)
            or (
                original_hash is not None
                and (
                    not isinstance(original_mode, int)
                    or isinstance(original_mode, bool)
                    or not 0 <= original_mode <= 0o777
                )
            )
            or not isinstance(new_mode, int)
            or isinstance(new_mode, bool)
            or not 0 <= new_mode <= 0o777
        ):
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL", f"journal write {index} has invalid file modes"
            )
        if (
            folded.startswith(".raw/")
            and folded != ".raw/.manifest.json"
            and mode != "create"
        ):
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL",
                f"journal write {index} violates raw create-only policy",
            )

        backup_content: bytes | None = None
        if original_hash is not None:
            if backup_fd is None:
                raise TransactionRecoveryError(
                    "CORRUPT_JOURNAL", "required transaction backups are unavailable"
                )
            backup_content = _read_recovery_backup_at(
                backup_fd,
                expected_backup,
                label=f"transaction backup {expected_backup}",
            )
            total_backup_bytes += len(backup_content)
            if total_backup_bytes > MAX_RECOVERY_TOTAL_BACKUP_BYTES:
                raise TransactionRecoveryError(
                    "CORRUPT_JOURNAL",
                    "transaction backups exceed the recovery size limit",
                )
            if sha256_bytes(backup_content) != original_hash:
                raise TransactionRecoveryError(
                    "CORRUPT_JOURNAL",
                    f"transaction backup {expected_backup} is corrupt",
                )
        elif (
            backup_fd is not None
            and _runtime_entry_metadata(backup_fd, expected_backup) is not None
        ):
            raise TransactionRecoveryError(
                "CORRUPT_JOURNAL",
                f"create-only journal write {index} has an unexpected backup",
            )
        validated.append(
            RecoveryWrite(
                relative_path=relative,
                original_sha256=original_hash,
                new_sha256=new_hash,
                original_mode=original_mode,
                new_mode=new_mode,
                backup_content=backup_content,
            )
        )
    return validated


def _restore_journal(
    vault_root: Path,
    transaction: _OperationStore,
    journal: dict[str, Any],
    *,
    root_fd: int | Path,
    meta_fd: int | Path,
) -> None:
    writes = _validated_recovery_writes(
        vault_root,
        transaction,
        journal,
        root_fd=root_fd,
        meta_fd=meta_fd,
    )
    failures: list[str] = []

    def record_failure(relative: str, detail: object) -> None:
        # Journal diagnostics are durable but must remain inside the envelope
        # preflighted before mutation. Paths are already byte-bounded.
        failures.append(f"{relative}: {str(detail)[:1024]}")

    for entry in reversed(writes):
        try:
            relative = entry.relative_path
            original_hash = entry.original_sha256
            current_hash = _safe_hash(
                vault_root, relative, root_fd=root_fd, meta_fd=meta_fd
            )
            new_hash = entry.new_sha256
            if current_hash not in {original_hash, new_hash, None}:
                record_failure(relative, "content changed outside transaction")
                continue
            if original_hash is None:
                if current_hash is None:
                    continue
                if current_hash == new_hash and isinstance(new_hash, str):
                    _confined_vault_unlink(
                        vault_root,
                        relative,
                        expected_sha256=new_hash,
                        root_fd=root_fd,
                        meta_fd=meta_fd,
                    )
            else:
                assert entry.backup_content is not None
                _atomic_vault_write(
                    vault_root,
                    relative,
                    entry.backup_content,
                    mode=entry.original_mode,
                    root_fd=root_fd,
                    meta_fd=meta_fd,
                )
        except Exception as exc:  # recovery must aggregate every path failure
            record_failure(entry.relative_path, exc)
    if failures:
        journal["state"] = "rollback-failed"
        journal["recovery_failures"] = failures
        transaction.write_json("journal.json", journal)
        raise TransactionRecoveryError("ROLLBACK_FAILED", "; ".join(failures))
    journal["state"] = "rolled-back"
    journal["rolled_back_epoch"] = time.time()
    transaction.write_json("journal.json", journal)


def _validate_completed_result(
    vault_root: Path,
    result: Mapping[str, Any],
    *,
    error_type: type[TransactionError],
    code: str,
    root_fd: int | Path | None = None,
    meta_fd: int | Path | None = None,
) -> None:
    paths = result.get("changed_paths")
    hashes = result.get("hashes")
    modes = result.get("modes")
    if (
        not isinstance(paths, list)
        or any(not isinstance(path, str) for path in paths)
        or len(set(paths)) != len(paths)
        or not isinstance(hashes, dict)
        or not isinstance(modes, dict)
        or set(paths) != set(hashes)
        or set(paths) != set(modes)
    ):
        raise error_type(
            code, "completed operation result has invalid path/hash/mode coverage"
        )
    for relative in paths:
        expected_hash = hashes[relative]
        expected_mode = modes[relative]
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
            or not isinstance(expected_mode, int)
            or isinstance(expected_mode, bool)
            or not 0 <= expected_mode <= 0o777
        ):
            raise error_type(
                code, f"completed operation result is invalid for {relative}"
            )
        try:
            actual_hash, actual_mode = _safe_file_state(
                vault_root, relative, root_fd=root_fd, meta_fd=meta_fd
            )
        except TransactionError as exc:
            raise error_type(
                code, f"cannot verify completed path {relative}: {exc}"
            ) from exc
        if actual_hash != expected_hash or actual_mode != expected_mode:
            raise error_type(
                code,
                f"completed operation path drifted: {relative} "
                f"(expected sha256={expected_hash} mode={expected_mode:04o}, "
                f"found sha256={actual_hash} mode={actual_mode})",
            )


def _assert_transaction_namespaces(
    lock: MutationLock,
    runtime: _RuntimeStore,
    operation: _OperationStore | None = None,
) -> None:
    lock.assert_vault_root_current()
    lock.assert_runtime_namespace_current()
    runtime.assert_current()
    if operation is not None:
        operation.assert_current()


def _validate_result_journal_correlation(
    operation_name: str,
    result: Mapping[str, Any],
    journal: Mapping[str, Any],
    writes: Sequence[RecoveryWrite],
) -> None:
    expected_paths = [entry.relative_path for entry in writes]
    expected_hashes = {entry.relative_path: entry.new_sha256 for entry in writes}
    expected_modes = {entry.relative_path: entry.new_mode for entry in writes}
    expected_fields = {
        "operation_type": journal.get("operation_type"),
        "bundle_sha256": journal.get("input_bundle_sha256"),
        "expanded_bundle_sha256": journal.get("expanded_bundle_sha256"),
        "approval_sha256": journal.get("approval_sha256"),
    }
    if (
        any(not isinstance(value, str) for value in expected_fields.values())
        or any(result.get(key) != value for key, value in expected_fields.items())
        or result.get("changed_paths") != expected_paths
        or result.get("hashes") != expected_hashes
        or result.get("modes") != expected_modes
    ):
        raise TransactionRecoveryError(
            "CORRUPT_RESULT",
            f"transaction {operation_name} result does not match its journal",
        )


def _recover_incomplete_locked(
    vault_root: Path,
    lock: MutationLock,
    runtime: _RuntimeStore,
) -> list[str]:
    recovered: list[str] = []
    _assert_transaction_namespaces(lock, runtime)
    for operation_name in runtime.operation_names():
        safe_operation_id(operation_name)
        if runtime.transactions_fd is None:
            break
        metadata = _runtime_entry_metadata(runtime.transactions_fd, operation_name)
        if metadata is None:
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise TransactionRecoveryError(
                "CORRUPT_RUNTIME_STATE", f"unsafe transaction entry: {operation_name}"
            )
        operation = runtime.open_operation(operation_name, create=False)
        if operation is None:
            continue
        with operation:
            _assert_transaction_namespaces(lock, runtime, operation)
            journal_exists = operation.exists("journal.json")
            result_exists = operation.exists("changed-paths.json")
            if not journal_exists:
                if result_exists:
                    operation.read_json(
                        "changed-paths.json",
                        label=f"transaction {operation_name} result",
                    )
                    _assert_transaction_namespaces(lock, runtime, operation)
                    continue
                runtime.remove_operation(operation)
                recovered.append(operation_name)
                continue
            journal = operation.read_json(
                "journal.json", label=f"transaction {operation_name} journal"
            )
            _assert_transaction_namespaces(lock, runtime, operation)
            if not isinstance(journal, dict) or journal.get("schema") != JOURNAL_SCHEMA:
                raise TransactionRecoveryError(
                    "CORRUPT_JOURNAL",
                    f"transaction {operation_name} has an invalid journal",
                )
            if result_exists:
                writes = _validated_recovery_writes(
                    vault_root,
                    operation,
                    journal,
                    root_fd=runtime.root_fd,
                    meta_fd=runtime.meta_fd,
                )
                result = operation.read_json(
                    "changed-paths.json",
                    label=f"transaction {operation_name} result",
                )
                if (
                    not isinstance(result, dict)
                    or result.get("schema") != RESULT_SCHEMA
                    or result.get("status") != "complete"
                    or result.get("operation_id") != operation_name
                ):
                    raise TransactionRecoveryError(
                        "CORRUPT_RESULT",
                        f"transaction {operation_name} has an invalid result",
                    )
                _validate_result_journal_correlation(
                    operation_name, result, journal, writes
                )
                if journal.get("state") != "complete":
                    _validate_completed_result(
                        vault_root,
                        result,
                        error_type=TransactionRecoveryError,
                        code="RESULT_DRIFT",
                        root_fd=runtime.root_fd,
                        meta_fd=runtime.meta_fd,
                    )
                    journal["state"] = "complete"
                    journal["completed_epoch"] = time.time()
                    operation.write_json("journal.json", journal)
                    recovered.append(operation_name)
                _assert_transaction_namespaces(lock, runtime, operation)
                continue
            if journal.get("state") in {"prepared", "applying", "rollback-failed"}:
                _restore_journal(
                    vault_root,
                    operation,
                    journal,
                    root_fd=runtime.root_fd,
                    meta_fd=runtime.meta_fd,
                )
                recovered.append(operation_name)
            elif journal.get("state") == "complete":
                input_hash = journal.get("input_bundle_sha256")
                expanded_hash = journal.get("expanded_bundle_sha256")
                approval_hash = journal.get("approval_sha256")
                if (
                    not isinstance(input_hash, str)
                    or not isinstance(expanded_hash, str)
                    or not isinstance(approval_hash, str)
                ):
                    raise TransactionRecoveryError(
                        "COMPLETE_RESULT_MISSING",
                        f"transaction {operation_name} completed without a recoverable result",
                    )
                writes = _validated_recovery_writes(
                    vault_root,
                    operation,
                    journal,
                    root_fd=runtime.root_fd,
                    meta_fd=runtime.meta_fd,
                )
                hashes = {entry.relative_path: entry.new_sha256 for entry in writes}
                if any(
                    _safe_hash(
                        vault_root,
                        path,
                        root_fd=runtime.root_fd,
                        meta_fd=runtime.meta_fd,
                    )
                    != digest
                    for path, digest in hashes.items()
                ):
                    raise TransactionRecoveryError(
                        "COMPLETE_RESULT_MISSING",
                        f"transaction {operation_name} result cannot be reconstructed",
                    )
                result = {
                    "schema": RESULT_SCHEMA,
                    "operation_id": operation_name,
                    "operation_type": journal.get("operation_type"),
                    "bundle_sha256": input_hash,
                    "expanded_bundle_sha256": expanded_hash,
                    "approval_sha256": approval_hash,
                    "status": "complete",
                    "changed_paths": list(hashes),
                    "hashes": hashes,
                    "modes": {entry.relative_path: entry.new_mode for entry in writes},
                }
                _validate_completed_result(
                    vault_root,
                    result,
                    error_type=TransactionRecoveryError,
                    code="RESULT_DRIFT",
                    root_fd=runtime.root_fd,
                    meta_fd=runtime.meta_fd,
                )
                operation.write_json("changed-paths.json", result)
                recovered.append(operation_name)
            _assert_transaction_namespaces(lock, runtime, operation)
    return recovered


def recover_incomplete(
    vault_root: Path,
    *,
    mutation_lock: MutationLock | None = None,
) -> list[str]:
    """Recover interrupted operations using only lock-derived directory descriptors."""

    vault = canonical(vault_root)
    if mutation_lock is None:
        with MutationLock(vault) as held_lock:
            with _RuntimeStore.from_lock(held_lock, create=False) as runtime:
                return _recover_incomplete_locked(vault, held_lock, runtime)
    if mutation_lock.vault_root != vault or not mutation_lock.acquired:
        raise TransactionError(
            "LOCK_NOT_HELD", "recovery requires the held lock for the selected vault"
        )
    with _RuntimeStore.from_lock(mutation_lock, create=False) as runtime:
        return _recover_incomplete_locked(vault, mutation_lock, runtime)


def inspect_bundle(
    vault_root: Path | str,
    bundle_or_path: Mapping[str, Any] | Path | str,
) -> dict[str, Any]:
    """Validate and summarize a bundle without mutating the vault."""

    vault = canonical(vault_root)
    if vault.exists() and not vault.is_dir():
        raise TransactionValidationError(
            "VAULT_NOT_DIRECTORY", f"vault is not a directory: {vault}"
        )
    if not vault.exists() and not vault.parent.is_dir():
        raise TransactionValidationError(
            "VAULT_PARENT_MISSING", f"vault parent does not exist: {vault.parent}"
        )
    _assert_no_portable_vault_root_alias(vault)
    bundle, bundle_dir = _load_bundle(bundle_or_path)
    if bundle.get("schema") != BUNDLE_SCHEMA:
        raise TransactionValidationError(
            "INVALID_BUNDLE_SCHEMA", f"bundle must declare schema={BUNDLE_SCHEMA!r}"
        )
    operation_id = safe_operation_id(bundle.get("operation_id"))
    operation_type = bundle.get("operation_type")
    if operation_type not in OPERATION_TYPES:
        raise TransactionValidationError(
            "INVALID_OPERATION_TYPE", f"unsupported operation_type: {operation_type!r}"
        )
    root_fd: int | None = None
    if vault.exists() and _supports_confined_dirfd():
        try:
            root_fd = os.open(vault, directory_open_flags())
        except OSError as exc:
            raise TransactionValidationError(
                "UNSAFE_VAULT_IDENTITY", f"cannot pin vault directory: {exc}"
            ) from exc
    vault_identity = _vault_object_identity(vault, root_fd=root_fd)
    try:
        _safe_directory(vault, ".vault-meta", create=False)
        expanded = _expand_managed_metadata(vault, bundle, bundle_dir, root_fd=root_fd)
        with tempfile.TemporaryDirectory(
            prefix="codex-brain-inspect-"
        ) as directory:
            prepared = _prepare_writes(
                vault,
                expanded,
                bundle_dir,
                Path(directory),
                root_fd=root_fd,
            )
        recheck_identity = root_fd is not None or not _supports_confined_dirfd()
        if recheck_identity and _vault_object_identity(vault) != vault_identity:
            raise TransactionValidationError(
                "VAULT_IDENTITY_CHANGED", "vault changed while the plan was inspected"
            )
    finally:
        if root_fd is not None:
            os.close(root_fd)
    expanded_hash = _canonical_json_hash(expanded)
    return {
        "schema": "codex-brain.transaction-plan.v1",
        "operation_id": operation_id,
        "operation_type": operation_type,
        "valid": True,
        "changed_paths": [write.relative_path for write in prepared],
        "hashes": {write.relative_path: write.content_sha256 for write in prepared},
        "modes": {write.relative_path: write.new_mode for write in prepared},
        "input_bundle_sha256": _canonical_json_hash(bundle),
        "expanded_bundle_sha256": expanded_hash,
        "vault_identity": vault_identity,
        "approval_sha256": plan_approval_sha256(
            vault, expanded, prepared, vault_identity=vault_identity
        ),
    }


def apply_bundle(
    vault_root: Path | str,
    bundle_or_path: Mapping[str, Any] | Path | str,
    *,
    timeout: float = 10.0,
    stale_after: float = 3600.0,
    fail_after: int | None = None,
    progress: Callable[[str, int], None] | None = None,
    approved_plan_sha256: str | None = None,
    reviewed_vault_identity: Mapping[str, Any] | None = None,
    expected_current_vault_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _require_write_platform(vault_root)
    vault = canonical(vault_root)
    if not vault.is_dir():
        raise TransactionValidationError(
            "VAULT_MISSING", f"vault does not exist: {vault}"
        )
    bundle, bundle_dir = _load_bundle(bundle_or_path)
    if bundle.get("schema") != BUNDLE_SCHEMA:
        raise TransactionValidationError(
            "INVALID_BUNDLE_SCHEMA", f"bundle must declare schema={BUNDLE_SCHEMA!r}"
        )
    operation_id = safe_operation_id(bundle.get("operation_id"))
    operation_type = bundle.get("operation_type")
    if operation_type not in OPERATION_TYPES:
        raise TransactionValidationError(
            "INVALID_OPERATION_TYPE", f"unsupported operation_type: {operation_type!r}"
        )
    input_bundle_hash = _canonical_json_hash(bundle)

    # Reject a stale/wrong approval without creating runtime state whenever
    # this is not an idempotent replay of an already-recorded operation.
    preview_result = (
        vault / ".vault-meta" / "transactions" / operation_id / "changed-paths.json"
    )
    if (reviewed_vault_identity is None) != (expected_current_vault_identity is None):
        raise TransactionValidationError(
            "INVALID_VAULT_TRANSITION",
            "reviewed and current vault identities must be supplied together",
        )
    if reviewed_vault_identity is not None and (
        operation_type != "setup"
        or not _valid_absent_vault_identity(reviewed_vault_identity, leaf=vault.name)
        or expected_current_vault_identity is None
        or not _valid_existing_vault_identity(expected_current_vault_identity)
    ):
        raise TransactionValidationError(
            "INVALID_VAULT_TRANSITION",
            "identity transitions are permitted only from the reviewed absent Init root",
        )
    if reviewed_vault_identity is not None and approved_plan_sha256 is None:
        raise TransactionValidationError(
            "PLAN_APPROVAL_REQUIRED",
            "a reviewed vault identity requires its approved plan hash",
        )
    if (
        approved_plan_sha256 is not None
        and reviewed_vault_identity is None
        and not preview_result.is_file()
    ):
        prelock_approval = inspect_bundle(vault, bundle_or_path)["approval_sha256"]
        if not hmac.compare_digest(approved_plan_sha256, str(prelock_approval)):
            raise TransactionValidationError(
                "PLAN_CHANGED",
                "the transaction or selected vault differs from the reviewed approval_sha256",
            )

    vault_label = str(vault)
    with MutationLock(
        vault,
        timeout=timeout,
        stale_after=stale_after,
        expected_root_parent_identity=reviewed_vault_identity,
    ) as mutation_lock:
        with _RuntimeStore.from_lock(mutation_lock, create=True) as runtime:
            _assert_transaction_namespaces(mutation_lock, runtime)
            current_vault_identity = _vault_object_identity(
                vault, root_fd=runtime.root_fd
            )
            if (
                expected_current_vault_identity is not None
                and dict(expected_current_vault_identity) != current_vault_identity
            ):
                raise TransactionValidationError(
                    "PLAN_CHANGED",
                    "the current vault directory object differs from the apply transition",
                )
            if reviewed_vault_identity is not None:
                if mutation_lock._root_parent_fd is not None:
                    parent_identity = os.fstat(mutation_lock._root_parent_fd)
                    parent_device = parent_identity.st_dev
                    parent_inode = parent_identity.st_ino
                else:
                    # COMPATIBLE tier: no pinned parent fd -- the accepted
                    # narrower point-in-time lstat, same tradeoff as every
                    # other degraded-mode identity check in this file.
                    try:
                        parent_identity = assert_unaliased_directory(
                            mutation_lock.vault_root.parent
                        )
                    except OSError as exc:
                        raise TransactionValidationError(
                            "PLAN_CHANGED",
                            f"cannot verify the reviewed absent vault slot: {exc}",
                        ) from exc
                    parent_device = parent_identity.st_dev
                    parent_inode = parent_identity.st_ino
                if (
                    reviewed_vault_identity["parent_device"] != parent_device
                    or reviewed_vault_identity["parent_inode"] != parent_inode
                    or reviewed_vault_identity["leaf"] != mutation_lock._root_name
                ):
                    raise TransactionValidationError(
                        "PLAN_CHANGED",
                        "the reviewed absent vault slot differs from the apply transition",
                    )
            approval_vault_identity = (
                dict(reviewed_vault_identity)
                if reviewed_vault_identity is not None
                else current_vault_identity
            )
            prior_operation = runtime.open_operation(operation_id, create=False)
            if prior_operation is not None:
                with prior_operation:
                    if prior_operation.exists("changed-paths.json"):
                        prior = prior_operation.read_json(
                            "changed-paths.json",
                            label=f"transaction {operation_id} result",
                        )
                        if (
                            not isinstance(prior, dict)
                            or prior.get("schema") != RESULT_SCHEMA
                        ):
                            raise TransactionRecoveryError(
                                "CORRUPT_RESULT",
                                f"prior operation result is invalid: {operation_id}",
                            )
                        if prior.get("bundle_sha256") != input_bundle_hash:
                            raise TransactionConflict(
                                "OPERATION_ID_REUSED",
                                f"operation_id {operation_id!r} already completed with another bundle",
                            )
                        if approved_plan_sha256 is not None and not hmac.compare_digest(
                            approved_plan_sha256,
                            str(prior.get("approval_sha256", "")),
                        ):
                            raise TransactionValidationError(
                                "PLAN_CHANGED",
                                "the completed transaction differs from the approved vault-bound plan",
                            )
                        _validate_completed_result(
                            vault,
                            prior,
                            error_type=TransactionConflict,
                            code="OPERATION_RESULT_DRIFT",
                            root_fd=runtime.root_fd,
                            meta_fd=runtime.meta_fd,
                        )
                        _assert_transaction_namespaces(
                            mutation_lock, runtime, prior_operation
                        )
                        _recover_incomplete_locked(vault, mutation_lock, runtime)
                        return prior

            _recover_incomplete_locked(vault, mutation_lock, runtime)
            _assert_transaction_namespaces(mutation_lock, runtime)
            bundle = _expand_managed_metadata(
                vault,
                bundle,
                bundle_dir,
                root_fd=runtime.root_fd,
                meta_fd=runtime.meta_fd,
            )
            expanded_bundle_hash = _canonical_json_hash(bundle)

            existing = runtime.open_operation(operation_id, create=False)
            if existing is not None:
                with existing:
                    journal = existing.read_json(
                        "journal.json", label=f"transaction {operation_id} journal"
                    )
                    if (
                        isinstance(journal, dict)
                        and journal.get("state") == "rolled-back"
                        and journal.get("input_bundle_sha256") == input_bundle_hash
                    ):
                        runtime.remove_operation(existing)
                    else:
                        raise TransactionRecoveryError(
                            "INCOMPLETE_TRANSACTION_DIRECTORY",
                            "transaction directory exists without a recoverable result: "
                            f"{operation_id}",
                        )

            operation = runtime.open_operation(operation_id, create=True)
            if operation is None:
                raise TransactionRecoveryError(
                    "CORRUPT_RUNTIME_STATE", "cannot create transaction runtime"
                )
            with operation:
                backups_fd = operation.open_backups(create=True)
                if backups_fd is None:
                    raise TransactionRecoveryError(
                        "CORRUPT_RUNTIME_STATE", "cannot create transaction backups"
                    )
                try:
                    prepared = _prepare_writes(
                        vault,
                        bundle,
                        bundle_dir,
                        None,
                        backups_fd=backups_fd,
                        root_fd=runtime.root_fd,
                        meta_fd=runtime.meta_fd,
                    )
                    _assert_transaction_namespaces(mutation_lock, runtime, operation)
                    approval_hash = plan_approval_sha256(
                        vault,
                        bundle,
                        prepared,
                        vault_label=vault_label,
                        vault_identity=approval_vault_identity,
                    )
                    if approved_plan_sha256 is not None and not hmac.compare_digest(
                        approved_plan_sha256, approval_hash
                    ):
                        raise TransactionValidationError(
                            "PLAN_CHANGED",
                            "the transaction, file modes, or selected vault differs from the reviewed approval_sha256",
                        )
                    journal = _journal_for(
                        operation_id,
                        operation_type,
                        prepared,
                        input_bundle_hash=input_bundle_hash,
                        expanded_bundle_hash=expanded_bundle_hash,
                        approval_hash=approval_hash,
                    )
                    result = _result_for(
                        operation_id,
                        operation_type,
                        prepared,
                        input_bundle_hash=input_bundle_hash,
                        expanded_bundle_hash=expanded_bundle_hash,
                        approval_hash=approval_hash,
                    )
                    _validate_journal_envelope(journal, prepared)
                    _validate_runtime_document_size(
                        result, label="completed-operation result"
                    )
                    operation.write_bundle(bundle)
                    _assert_transaction_namespaces(mutation_lock, runtime, operation)
                except BaseException:
                    runtime.remove_operation(operation)
                    raise

                operation.write_json("journal.json", journal)
                journal["state"] = "applying"
                operation.write_json("journal.json", journal)
                _assert_transaction_namespaces(mutation_lock, runtime, operation)

                try:
                    for index, write in enumerate(prepared, start=1):
                        _assert_transaction_namespaces(
                            mutation_lock, runtime, operation
                        )
                        current_hash, current_mode = _safe_file_state(
                            vault,
                            write.relative_path,
                            root_fd=runtime.root_fd,
                            meta_fd=runtime.meta_fd,
                        )
                        if current_hash != write.original_sha256:
                            raise TransactionConflict(
                                "EXPECTED_HASH_MISMATCH",
                                f"{write.relative_path} changed before it could be applied",
                            )
                        if current_mode != write.original_mode:
                            raise TransactionConflict(
                                "EXPECTED_MODE_MISMATCH",
                                f"{write.relative_path} mode changed before it could be applied",
                            )
                        _assert_no_existing_portable_alias(
                            vault,
                            write.relative_path,
                            root_fd=runtime.root_fd,
                            meta_fd=runtime.meta_fd,
                        )
                        _atomic_vault_write(
                            vault,
                            write.relative_path,
                            write.content,
                            mode=write.new_mode,
                            root_fd=runtime.root_fd,
                            meta_fd=runtime.meta_fd,
                        )
                        journal["applied"].append(write.relative_path)
                        _assert_no_existing_portable_alias(
                            vault,
                            write.relative_path,
                            root_fd=runtime.root_fd,
                            meta_fd=runtime.meta_fd,
                        )
                        operation.write_json("journal.json", journal)
                        _assert_transaction_namespaces(
                            mutation_lock, runtime, operation
                        )
                        if progress is not None:
                            progress(write.relative_path, index)
                            _assert_transaction_namespaces(
                                mutation_lock, runtime, operation
                            )
                        if fail_after is not None and index == fail_after:
                            raise RuntimeError(f"injected failure after write {index}")
                except BaseException:
                    _restore_journal(
                        vault,
                        operation,
                        journal,
                        root_fd=runtime.root_fd,
                        meta_fd=runtime.meta_fd,
                    )
                    raise

                try:
                    _validate_completed_result(
                        vault,
                        result,
                        error_type=TransactionRecoveryError,
                        code="RESULT_DRIFT",
                        root_fd=runtime.root_fd,
                        meta_fd=runtime.meta_fd,
                    )
                    _assert_transaction_namespaces(mutation_lock, runtime, operation)
                except BaseException:
                    _restore_journal(
                        vault,
                        operation,
                        journal,
                        root_fd=runtime.root_fd,
                        meta_fd=runtime.meta_fd,
                    )
                    raise
                operation.write_json("changed-paths.json", result)
                journal["state"] = "complete"
                journal["completed_epoch"] = time.time()
                operation.write_json("journal.json", journal)
                _assert_transaction_namespaces(mutation_lock, runtime, operation)
                return result
