from __future__ import annotations

import os

import pytest

from engine.paths import (
    canonical,
    capability_for,
    is_adoptable_vault,
    is_cloud_placeholder,
    is_initialized_vault,
    is_same_object,
)
from engine.hostplatform.capability import GuaranteeTier


def test_canonical_roundtrip(tmp_path):
    nested = tmp_path / "a" / ".." / "a" / "b"
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert canonical(nested) == canonical(tmp_path / "a" / "b")


def test_tmp_vault_fixture_is_initialized_and_adoptable(tmp_vault):
    assert is_initialized_vault(tmp_vault)
    assert is_adoptable_vault(tmp_vault)


class _FakeStat:
    def __init__(self, st_dev, st_ino):
        self.st_dev = st_dev
        self.st_ino = st_ino


class _FakeWindowsIdentity:
    def __init__(self, tup, stable=True):
        self._tup = tup
        self._stable = stable

    def as_tuple(self):
        return self._tup

    def is_stable(self):
        return self._stable


def test_is_same_object_posix_stat_result_equal():
    assert is_same_object(_FakeStat(1, 2), _FakeStat(1, 2))


def test_is_same_object_posix_stat_result_zero_inode_never_equal():
    assert not is_same_object(_FakeStat(1, 0), _FakeStat(1, 0))


def test_is_same_object_posix_stat_result_different_inode():
    assert not is_same_object(_FakeStat(1, 2), _FakeStat(1, 3))


def test_is_same_object_windows_identity_equal():
    left = _FakeWindowsIdentity((5, 10, 20))
    right = _FakeWindowsIdentity((5, 10, 20))
    assert is_same_object(left, right)


def test_is_same_object_windows_identity_unstable_never_equal():
    left = _FakeWindowsIdentity((0, 0, 0), stable=False)
    right = _FakeWindowsIdentity((0, 0, 0), stable=False)
    assert not is_same_object(left, right)


def test_is_same_object_rejects_unrecognized_object():
    with pytest.raises(TypeError):
        is_same_object(object(), object())


def test_is_cloud_placeholder_true_for_cloud_tag():
    stat = _FakeStat(1, 2)
    stat.st_reparse_tag = 0x9000101A
    assert is_cloud_placeholder(stat)


def test_is_cloud_placeholder_false_for_symlink_tag():
    stat = _FakeStat(1, 2)
    stat.st_reparse_tag = 0xA000000C  # IO_REPARSE_TAG_SYMLINK
    assert not is_cloud_placeholder(stat)


def test_is_cloud_placeholder_false_when_no_reparse_tag():
    assert not is_cloud_placeholder(_FakeStat(1, 2))


def test_capability_for_posix(tmp_vault, posix_only):
    capability = capability_for(tmp_vault)
    assert capability.tier in (GuaranteeTier.STRICT, GuaranteeTier.UNSAFE_REFUSED)
    if os.name != "nt":
        # Linux/macOS dev and CI hosts support dir_fd confinement.
        assert capability.tier == GuaranteeTier.STRICT
        assert capability.descriptor_confined
