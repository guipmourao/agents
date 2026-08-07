from __future__ import annotations

import sys
import types

import pytest

from engine.hostplatform import fsclassify


@pytest.fixture(autouse=True)
def _clear_caches():
    fsclassify.classify_volume.cache_clear()
    fsclassify._hardlink_support_cache.clear()
    yield
    fsclassify.classify_volume.cache_clear()
    fsclassify._hardlink_support_cache.clear()


def _install_fake_win32file(monkeypatch, *, drive_type, fs_name, remote_marker):
    # GetVolumeInformation lives on win32api in real pywin32, not win32file
    # -- GetDriveType/DRIVE_REMOTE are the ones that are genuinely on
    # win32file. Mocking them on separate fake modules, matching the real
    # split, is what would have caught the AttributeError the second real
    # Windows CI run hit (this fixture originally put both on one fake
    # win32file, which happened to match the wrong assumption in the source
    # instead of catching it).
    fake_win32file = types.SimpleNamespace(
        DRIVE_REMOTE=remote_marker,
        GetDriveType=lambda root: drive_type,
    )
    fake_win32api = types.SimpleNamespace(
        GetVolumeInformation=lambda root: (None, None, None, None, fs_name),
    )
    monkeypatch.setitem(sys.modules, "win32file", fake_win32file)
    monkeypatch.setitem(sys.modules, "win32api", fake_win32api)
    return fake_win32file


def test_classify_volume_ntfs(monkeypatch):
    _install_fake_win32file(monkeypatch, drive_type=3, fs_name="NTFS", remote_marker=4)
    assert fsclassify.classify_volume("C:\\") == fsclassify.VolumeKind.NTFS_LOCAL


def test_classify_volume_refs(monkeypatch):
    _install_fake_win32file(monkeypatch, drive_type=3, fs_name="ReFS", remote_marker=4)
    assert fsclassify.classify_volume("D:\\") == fsclassify.VolumeKind.REFS_LOCAL


def test_classify_volume_fat32(monkeypatch):
    _install_fake_win32file(monkeypatch, drive_type=2, fs_name="FAT32", remote_marker=4)
    assert fsclassify.classify_volume("E:\\") == fsclassify.VolumeKind.FAT_EXFAT


def test_classify_volume_exfat(monkeypatch):
    _install_fake_win32file(monkeypatch, drive_type=2, fs_name="exFAT", remote_marker=4)
    assert fsclassify.classify_volume("F:\\") == fsclassify.VolumeKind.FAT_EXFAT


def test_classify_volume_remote_is_smb_before_fs_name_check(monkeypatch):
    fake = _install_fake_win32file(monkeypatch, drive_type=4, fs_name="NTFS", remote_marker=4)
    assert fsclassify.classify_volume("\\\\server\\share") == fsclassify.VolumeKind.SMB_NETWORK


def test_classify_volume_unknown_on_query_failure(monkeypatch):
    def _raise(root):
        raise OSError("no such volume")

    fake_win32file = types.SimpleNamespace(DRIVE_REMOTE=4, GetDriveType=lambda root: 3)
    fake_win32api = types.SimpleNamespace(GetVolumeInformation=_raise)
    monkeypatch.setitem(sys.modules, "win32file", fake_win32file)
    monkeypatch.setitem(sys.modules, "win32api", fake_win32api)
    assert fsclassify.classify_volume("Z:\\") == fsclassify.VolumeKind.UNKNOWN


def test_classify_volume_unknown_attribute_error_matches_real_ci_failure(monkeypatch):
    """Regression test: the second real windows-latest CI run hit
    AttributeError('win32file' has no attribute 'GetVolumeInformation')
    because that function is actually on win32api. A fake win32api module
    missing the attribute entirely reproduces that exact failure shape."""

    fake_win32file = types.SimpleNamespace(DRIVE_REMOTE=4, GetDriveType=lambda root: 3)
    fake_win32api = types.SimpleNamespace()  # no GetVolumeInformation at all
    monkeypatch.setitem(sys.modules, "win32file", fake_win32file)
    monkeypatch.setitem(sys.modules, "win32api", fake_win32api)
    assert fsclassify.classify_volume("Y:\\") == fsclassify.VolumeKind.UNKNOWN


def test_detect_onedrive_root_matches(monkeypatch, tmp_path):
    onedrive = tmp_path / "OneDrive"
    vault = onedrive / "notes" / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("OneDrive", str(onedrive))
    monkeypatch.delenv("OneDriveCommercial", raising=False)
    monkeypatch.delenv("OneDriveConsumer", raising=False)
    assert fsclassify.detect_onedrive_root(vault) == onedrive.resolve()


def test_detect_onedrive_root_none_when_outside(monkeypatch, tmp_path):
    onedrive = tmp_path / "OneDrive"
    onedrive.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setenv("OneDrive", str(onedrive))
    monkeypatch.delenv("OneDriveCommercial", raising=False)
    monkeypatch.delenv("OneDriveConsumer", raising=False)
    assert fsclassify.detect_onedrive_root(outside) is None


def test_supports_hardlinks_true_on_a_filesystem_that_supports_them(tmp_path):
    # Real filesystem probe (no win32 mocking) -- ext4/most CI filesystems
    # support hardlinks, exercising the same code path os.link would use.
    assert fsclassify.supports_hardlinks(str(tmp_path)) is True
