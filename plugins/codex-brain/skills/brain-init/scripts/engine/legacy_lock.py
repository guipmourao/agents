"""Race-safe compatibility implementation for the deprecated v1 page locks.

The persistent ``.vault-meta/locks/*.lock`` records intentionally retain the
v1 wire format because old integrations acquire and release them in separate
processes.  Mutating commands are serialized by the canonical vault mutation
lock.  Every legacy runtime lookup and mutation is relative to pinned directory
descriptors, so a vault-controlled symlink swap cannot redirect an operation.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, Sequence

from .paths import VaultSelectionError, assert_not_plugin_tree, canonical, is_name_surrogate
from .hostplatform import dirops
from .transaction import (
    MutationLock,
    TransactionConflict,
    TransactionError,
    TransactionValidationError,
)


DEFAULT_STALE_AFTER_SECONDS = 60
DEFAULT_CLEAR_AFTER_SECONDS = 3600
MAX_DECIMAL_DIGITS = 18
MAX_RECORD_BYTES = 64 * 1024
MAX_PATH_BYTES = 1024
MAX_PATH_COMPONENT_BYTES = 255
LOCK_TIMEOUT_SECONDS = 5.0
MAX_LOCK_DIRECTORY_ENTRIES = 4096
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_HEX_SHA1_LOCK = re.compile(r"^[0-9a-f]{40}\.lock$")


USAGE = """wiki-lock.sh -- deprecated v1 compatibility lock helper

Usage:
  wiki-lock.sh [--stale-after-sec N] acquire <vault-rel-path>
  wiki-lock.sh release <vault-rel-path>
  wiki-lock.sh list
  wiki-lock.sh clear-stale [--max-age N | N]
  wiki-lock.sh peek <vault-rel-path>

Exit codes: 0 success, 2 usage/vault selection, 3 unsafe runtime path,
4 invalid vault-relative path, 75 held/temporarily unavailable.
"""


class LegacyLockError(RuntimeError):
    """One stable compatibility failure with its historical exit status."""

    def __init__(self, code: str, message: str, exit_code: int):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


@dataclass(frozen=True)
class LockRecord:
    pid: int
    epoch: int
    path: str

    def encode(self) -> bytes:
        return f"{self.pid} {self.epoch} {self.path}\n".encode("utf-8")


@dataclass(frozen=True)
class RecordRead:
    exists: bool
    record: LockRecord | None


@dataclass(frozen=True)
class ParsedArguments:
    command: str
    operands: tuple[str, ...]
    stale_after: int
    max_age: int | None


def _runtime_error(message: str) -> LegacyLockError:
    return LegacyLockError("UNSAFE_LEGACY_LOCK_RUNTIME", message, 3)


def _usage_error(message: str) -> LegacyLockError:
    return LegacyLockError("INVALID_LEGACY_LOCK_USAGE", message, 2)


def _path_error(message: str) -> LegacyLockError:
    return LegacyLockError("INVALID_LEGACY_LOCK_PATH", message, 4)


def _normalize_decimal(value: str, *, option: str) -> int:
    if (
        not value
        or len(value) > MAX_DECIMAL_DIGITS
        or any(character < "0" or character > "9" for character in value)
    ):
        raise _usage_error(
            f"{option} must be an unsigned decimal of at most "
            f"{MAX_DECIMAL_DIGITS} digits"
        )
    return int(value, 10)


def _parse_arguments(argv: Sequence[str]) -> ParsedArguments:
    if not argv:
        raise _usage_error("no command given")

    command = ""
    operands: list[str] = []
    stale_after = DEFAULT_STALE_AFTER_SECONDS
    max_age: int | None = None
    saw_stale = False
    saw_max_age = False
    index = 0
    positional_only = False
    while index < len(argv):
        value = argv[index]
        if not positional_only and value == "--":
            positional_only = True
            index += 1
            continue
        if not positional_only and value in {"-h", "--help"}:
            raise LegacyLockError("HELP", USAGE.rstrip(), 0)
        if not positional_only and value == "--stale-after-sec":
            if saw_stale:
                raise _usage_error("--stale-after-sec may be specified only once")
            if index + 1 >= len(argv):
                raise _usage_error("--stale-after-sec needs a value")
            stale_after = _normalize_decimal(
                argv[index + 1], option="--stale-after-sec"
            )
            saw_stale = True
            index += 2
            continue
        if not positional_only and value == "--max-age":
            if saw_max_age:
                raise _usage_error("--max-age may be specified only once")
            if index + 1 >= len(argv):
                raise _usage_error("--max-age needs a value")
            max_age = _normalize_decimal(argv[index + 1], option="--max-age")
            saw_max_age = True
            index += 2
            continue
        if not positional_only and value.startswith("-"):
            raise _usage_error(f"unknown flag: {value}")
        if not command:
            command = value
        else:
            operands.append(value)
        index += 1

    if not command:
        raise _usage_error("no command given")
    if command in {"acquire", "release", "peek"}:
        if len(operands) != 1:
            raise _usage_error(f"{command} needs exactly one path")
    elif command == "list":
        if operands:
            raise _usage_error("list does not accept positional arguments")
    elif command == "clear-stale":
        if len(operands) > 1:
            raise _usage_error("clear-stale accepts at most one positional max age")
        if operands and max_age is not None:
            raise _usage_error("choose either --max-age or a positional max age")
        if operands:
            max_age = _normalize_decimal(operands[0], option="clear-stale max age")
        if max_age is None:
            max_age = DEFAULT_CLEAR_AFTER_SECONDS
    else:
        raise _usage_error(
            f"unknown command: {command} (try acquire|release|list|clear-stale|peek)"
        )
    return ParsedArguments(command, tuple(operands), stale_after, max_age)


def _open_root(vault_root: Path) -> dirops.PinnedDirectory:
    """Open the vault root as a pinned directory, POSIX or Windows.

    On POSIX this still requires dir_fd confinement (STRICT tier), same as
    before; on Windows this now succeeds via ``hostplatform.windows_backend``
    (COMPATIBLE tier) instead of refusing outright. Callers of this function
    are all read-only (``list``/``peek``/path validation) -- the write path
    (``acquire``/``release``/``clear-stale``) goes through
    ``_serialized_lock_directory``/``MutationLock`` instead, which stays
    POSIX-only until that lock itself is ported.
    """

    try:
        return dirops.open_root(vault_root)
    except OSError as exc:
        raise _runtime_error(f"cannot open selected vault safely: {exc}") from exc


def _open_child_directory(
    parent: dirops.PinnedDirectory,
    name: str,
    *,
    create: bool,
) -> dirops.PinnedDirectory | None:
    try:
        return dirops.open_child(parent, name, create=create)
    except OSError as exc:
        raise _runtime_error(
            f"legacy runtime component is not a safe directory: {name}: {exc}"
        ) from exc


@contextmanager
def _lock_directory(
    vault_root: Path, *, create: bool
) -> Iterator[dirops.PinnedDirectory | None]:
    root = _open_root(vault_root)
    meta: dirops.PinnedDirectory | None = None
    locks: dirops.PinnedDirectory | None = None
    try:
        meta = _open_child_directory(root, ".vault-meta", create=create)
        if meta is None:
            yield None
            return
        locks = _open_child_directory(meta, "locks", create=create)
        yield locks
    finally:
        if locks is not None:
            dirops.close(locks)
        if meta is not None:
            dirops.close(meta)
        dirops.close(root)


@contextmanager
def _serialized_lock_directory(vault_root: Path) -> Iterator[dirops.PinnedDirectory]:
    try:
        with MutationLock(vault_root, timeout=LOCK_TIMEOUT_SECONDS) as mutation_lock:
            # Use the exact `.vault-meta` directory pinned by MutationLock.
            # Re-resolving it by pathname here would create a split-lock race
            # if that public entry were renamed and replaced between the two
            # opens. MutationLock is POSIX-only today, so `pinned_meta_fd` is
            # always a real dir_fd here -- wrapping it in PinnedDirectory(fd=)
            # keeps this adapter on the same dirops primitives as the
            # read-only paths above without changing behavior on POSIX.
            pinned_meta_fd = mutation_lock.duplicate_parent_fd()
            meta = dirops.PinnedDirectory(
                path=canonical(vault_root) / ".vault-meta", fd=pinned_meta_fd
            )
            locks: dirops.PinnedDirectory | None = None
            try:
                locks = _open_child_directory(meta, "locks", create=True)
                if locks is None:  # pragma: no cover - create=True is exhaustive
                    raise _runtime_error("cannot create legacy lock directory")
                yield locks
            finally:
                if locks is not None:
                    dirops.close(locks)
                dirops.close(meta)
    except TransactionConflict as exc:
        raise LegacyLockError(exc.code, str(exc), 75) from exc
    except TransactionValidationError as exc:
        raise LegacyLockError(exc.code, str(exc), 3) from exc
    except TransactionError as exc:
        raise LegacyLockError(exc.code, str(exc), exc.exit_code) from exc


def _normalized_relative_path(value: str) -> tuple[str, ...]:
    if not value:
        raise _path_error("path cannot be empty")
    if unicodedata.normalize("NFC", value) != value:
        raise _path_error("path must use NFC Unicode normalization")
    if len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise _path_error(f"path exceeds the {MAX_PATH_BYTES}-byte compatibility limit")
    if (
        "\\" in value
        or value.startswith("/")
        or _WINDOWS_DRIVE.match(value)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _path_error("path must be canonical vault-relative POSIX syntax")
    parsed = PurePosixPath(value)
    if (
        value in {".", ".."}
        or ".." in parsed.parts
        or parsed.as_posix() != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise _path_error(f"path must stay inside the vault: {value}")
    if any(
        len(part.encode("utf-8")) > MAX_PATH_COMPONENT_BYTES for part in parsed.parts
    ):
        raise _path_error(
            f"path component exceeds the {MAX_PATH_COMPONENT_BYTES}-byte portability limit"
        )
    return parsed.parts


def _validate_target_path(vault_root: Path, value: str) -> None:
    parts = _normalized_relative_path(value)
    directory = _open_root(vault_root)
    try:
        for index, part in enumerate(parts):
            try:
                metadata = dirops.stat_component(directory, part)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise _path_error(
                    f"cannot inspect vault-relative path {value}: {exc}"
                ) from exc
            # is_name_surrogate (not a bare S_ISLNK check) so a Windows
            # junction/mount point is rejected exactly like a POSIX symlink
            # -- on Windows, junctions lstat as ordinary directories
            # (S_ISLNK is False) but still redirect name lookups.
            if is_name_surrogate(metadata):
                raise _path_error(f"path may not traverse a symlink: {value}")
            if index == len(parts) - 1:
                return
            if not stat.S_ISDIR(metadata.st_mode):
                raise _path_error(f"path parent is not a directory: {value}")
            child = _open_child_directory(directory, part, create=False)
            if child is None:
                raise _path_error(f"cannot inspect vault-relative path {value}: missing")
            dirops.close(directory)
            directory = child
    finally:
        dirops.close(directory)


def _lock_name(path: str) -> str:
    # SHA-1 is the fixed v1 filename wire format, not a security primitive.
    digest = hashlib.sha1(path.encode("utf-8"), usedforsecurity=False)
    return f"{digest.hexdigest()}.lock"


def _portable_lock_key(path: str) -> str:
    """Collapse aliases that can name one file on supported vault volumes."""

    return unicodedata.normalize("NFC", path.casefold())


def _parse_record(raw: bytes, *, expected_name: str) -> LockRecord | None:
    if not raw or not raw.endswith(b"\n") or b"\n" in raw[:-1] or b"\r" in raw:
        return None
    try:
        fields = raw[:-1].decode("utf-8").split(" ", 2)
    except UnicodeDecodeError:
        return None
    if len(fields) != 3:
        return None
    pid_text, epoch_text, path = fields
    try:
        pid = _normalize_decimal(pid_text, option="record pid")
        epoch = _normalize_decimal(epoch_text, option="record epoch")
        _normalized_relative_path(path)
    except LegacyLockError:
        return None
    if _lock_name(path) != expected_name:
        return None
    return LockRecord(pid=pid, epoch=epoch, path=path)


def _read_record(directory: dirops.PinnedDirectory, name: str) -> RecordRead:
    raw = dirops.read_bounded_regular(directory, name, max_bytes=MAX_RECORD_BYTES)
    if raw is None:
        return RecordRead(exists=False, record=None)
    return RecordRead(exists=True, record=_parse_record(raw, expected_name=name))


def _unlink(directory: dirops.PinnedDirectory, name: str) -> bool:
    try:
        return dirops.unlink(directory, name)
    except OSError as exc:
        raise _runtime_error(f"cannot remove legacy lock record safely: {exc}") from exc


def _atomic_create(directory: dirops.PinnedDirectory, name: str, data: bytes) -> bool:
    try:
        return dirops.atomic_create(directory, name, data)
    except OSError as exc:
        raise _runtime_error(f"cannot create legacy lock record safely: {exc}") from exc


def _acquire(vault_root: Path, path: str, *, stale_after: int) -> int:
    _validate_target_path(vault_root, path)
    name = _lock_name(path)
    now = int(time.time())
    record = LockRecord(pid=os.getpid(), epoch=now, path=path)
    with _serialized_lock_directory(vault_root) as descriptor:
        existing = _read_record(descriptor, name)
        if existing.record is not None and now - existing.record.epoch <= stale_after:
            return 75
        # Preserve the exact SHA-1 v1 filename while conservatively excluding
        # case/Unicode aliases. Default macOS vault volumes resolve such names
        # to one file; allowing both locks would make safety host-dependent.
        requested_key = _portable_lock_key(path)
        for candidate in _record_names(descriptor):
            if candidate == name or not _HEX_SHA1_LOCK.fullmatch(candidate):
                continue
            collision = _read_record(descriptor, candidate).record
            if (
                collision is not None
                and _portable_lock_key(collision.path) == requested_key
                and now - collision.epoch <= stale_after
            ):
                return 75
        if existing.exists:
            _unlink(descriptor, name)
        return 0 if _atomic_create(descriptor, name, record.encode()) else 75


def _release(vault_root: Path, path: str) -> int:
    _validate_target_path(vault_root, path)
    with _serialized_lock_directory(vault_root) as descriptor:
        _unlink(descriptor, _lock_name(path))
    return 0


def _record_names(directory: dirops.PinnedDirectory) -> list[str]:
    try:
        return dirops.record_names(
            directory, suffix=".lock", max_entries=MAX_LOCK_DIRECTORY_ENTRIES
        )
    except OSError as exc:
        raise _runtime_error(
            f"cannot list legacy lock directory safely: {exc}"
        ) from exc


def _list(vault_root: Path) -> int:
    with _lock_directory(vault_root, create=False) as descriptor:
        if descriptor is None:
            return 0
        now = int(time.time())
        for name in _record_names(descriptor):
            if not _HEX_SHA1_LOCK.fullmatch(name):
                print("WARN: ignored corrupt legacy lock record", file=sys.stderr)
                continue
            record = _read_record(descriptor, name).record
            if record is None:
                print("WARN: ignored corrupt legacy lock record", file=sys.stderr)
                continue
            print(f"pid={record.pid} age={now - record.epoch}s path={record.path}")
    return 0


def _clear_stale(vault_root: Path, *, max_age: int) -> int:
    removed = 0
    now = int(time.time())
    with _serialized_lock_directory(vault_root) as descriptor:
        for name in _record_names(descriptor):
            read = _read_record(descriptor, name)
            if read.record is None or now - read.record.epoch > max_age:
                removed += int(_unlink(descriptor, name))
    print(removed)
    return 0


def _peek(vault_root: Path, path: str) -> int:
    _validate_target_path(vault_root, path)
    with _lock_directory(vault_root, create=False) as descriptor:
        if descriptor is None:
            print("unheld")
            return 0
        record = _read_record(descriptor, _lock_name(path)).record
        if record is None:
            print("unheld")
        else:
            sys.stdout.buffer.write(record.encode())
    return 0


def _select_vault(plugin_root: Path) -> Path:
    raw = os.environ.get("WIKI_LOCK_VAULT") or os.environ.get("CODEX_BRAIN")
    try:
        vault_root = canonical(raw if raw else Path.cwd())
    except OSError as exc:
        raise _usage_error(
            f"selected legacy lock vault cannot be resolved: {exc}"
        ) from exc
    try:
        metadata = vault_root.lstat()
    except OSError as exc:
        raise _usage_error("selected legacy lock vault is not a directory") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise _usage_error("selected legacy lock vault is not a directory")
    try:
        return assert_not_plugin_tree(vault_root, plugin_root, source="legacy-lock")
    except VaultSelectionError as exc:
        raise _usage_error(str(exc)) from exc


def run(argv: Sequence[str], *, plugin_root: Path) -> int:
    parsed = _parse_arguments(argv)
    vault_root = _select_vault(plugin_root)
    command = parsed.command
    if command == "acquire":
        return _acquire(vault_root, parsed.operands[0], stale_after=parsed.stale_after)
    if command == "release":
        return _release(vault_root, parsed.operands[0])
    if command == "list":
        return _list(vault_root)
    if command == "clear-stale":
        assert parsed.max_age is not None
        return _clear_stale(vault_root, max_age=parsed.max_age)
    if command == "peek":
        return _peek(vault_root, parsed.operands[0])
    raise AssertionError(f"unreachable command: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    plugin_root = Path(__file__).resolve().parents[1]
    try:
        return run(arguments, plugin_root=plugin_root)
    except LegacyLockError as exc:
        stream = sys.stdout if exc.exit_code == 0 else sys.stderr
        if exc.exit_code == 0:
            print(str(exc), file=stream)
        else:
            print(f"ERR: {exc}", file=stream)
        return exc.exit_code
    except (OSError, ValueError) as exc:
        print(f"ERR: legacy lock operation failed safely: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
