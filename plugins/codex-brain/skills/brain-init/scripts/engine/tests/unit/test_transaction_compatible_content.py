"""Coverage for the COMPATIBLE-tier (native Windows, root_fd: Path) branch of
transaction.py's per-file content-write functions, added in port phase 4h.

These functions' Path-mode branch is pure path/os operations with no
Windows-specific call inside it (the same degraded logic already used for
root_fd=None on a host without dir_fd support), so it runs for real here on
Linux by passing a Path as root_fd instead of None or a real fd.
"""

from __future__ import annotations

import pytest

from engine.transaction import (
    TransactionRecoveryError,
    TransactionValidationError,
    _assert_no_existing_portable_alias,
    _atomic_vault_write,
    _confined_vault_unlink,
    _safe_file_state,
    _safe_hash,
    _vault_object_identity,
    read_vault_regular,
)


def test_safe_hash_path_mode_missing_returns_none(tmp_vault):
    assert _safe_hash(tmp_vault, "wiki/missing.md", root_fd=tmp_vault) is None


def test_atomic_vault_write_and_safe_hash_path_mode_roundtrip(tmp_vault):
    _atomic_vault_write(
        tmp_vault, "wiki/note.md", b"hello", mode=0o644, root_fd=tmp_vault
    )
    target = tmp_vault / "wiki" / "note.md"
    assert target.read_bytes() == b"hello"
    digest = _safe_hash(tmp_vault, "wiki/note.md", root_fd=tmp_vault)
    import hashlib

    assert digest == hashlib.sha256(b"hello").hexdigest()


def test_safe_file_state_path_mode_roundtrip(tmp_vault):
    _atomic_vault_write(
        tmp_vault, "wiki/note.md", b"content", mode=0o644, root_fd=tmp_vault
    )
    digest, mode = _safe_file_state(tmp_vault, "wiki/note.md", root_fd=tmp_vault)
    assert digest is not None
    assert mode is not None


def test_read_vault_regular_path_mode_roundtrip(tmp_vault):
    _atomic_vault_write(
        tmp_vault, "wiki/note.md", b"payload", mode=0o644, root_fd=tmp_vault
    )
    assert read_vault_regular(tmp_vault, "wiki/note.md", root_fd=tmp_vault) == b"payload"


def test_read_vault_regular_path_mode_missing_ok(tmp_vault):
    assert read_vault_regular(tmp_vault, "wiki/missing.md", root_fd=tmp_vault) is None


def test_read_vault_regular_path_mode_missing_not_ok_raises(tmp_vault):
    with pytest.raises(TransactionValidationError):
        read_vault_regular(
            tmp_vault, "wiki/missing.md", missing_ok=False, root_fd=tmp_vault
        )


def test_confined_vault_unlink_path_mode(tmp_vault):
    _atomic_vault_write(
        tmp_vault, "wiki/note.md", b"payload", mode=0o644, root_fd=tmp_vault
    )
    digest = _safe_hash(tmp_vault, "wiki/note.md", root_fd=tmp_vault)
    _confined_vault_unlink(
        tmp_vault, "wiki/note.md", expected_sha256=digest, root_fd=tmp_vault
    )
    assert not (tmp_vault / "wiki" / "note.md").exists()


def test_confined_vault_unlink_path_mode_rejects_changed_content(tmp_vault):
    _atomic_vault_write(
        tmp_vault, "wiki/note.md", b"payload", mode=0o644, root_fd=tmp_vault
    )
    with pytest.raises(TransactionRecoveryError):
        _confined_vault_unlink(
            tmp_vault,
            "wiki/note.md",
            expected_sha256="0" * 64,
            root_fd=tmp_vault,
        )


def test_assert_no_existing_portable_alias_path_mode_allows_unique_name(tmp_vault):
    _assert_no_existing_portable_alias(tmp_vault, "wiki/unique.md", root_fd=tmp_vault)


def test_assert_no_existing_portable_alias_path_mode_rejects_casefold_alias(tmp_vault):
    (tmp_vault / "wiki" / "Note.md").write_text("x")
    with pytest.raises(TransactionValidationError):
        _assert_no_existing_portable_alias(tmp_vault, "wiki/note.md", root_fd=tmp_vault)


def test_vault_object_identity_path_mode(tmp_vault):
    identity = _vault_object_identity(tmp_vault, root_fd=tmp_vault)
    assert identity["state"] == "existing"
    assert isinstance(identity["device"], int)
    assert isinstance(identity["inode"], int)
