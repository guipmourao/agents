"""Filesystem classification for the native-Windows write path.

POSIX hosts don't need this: ``paths.supports_confined_dirfd`` plus the
existing zero-inode guard in ``paths.is_same_object`` already gate unsafe
filesystems (FAT/exFAT, unstable SMB) there. This module exists for Windows,
where ``paths.capability_for`` needs to know the underlying volume kind
before choosing between COMPATIBLE and UNSAFE_REFUSED, and for detecting the
two situations real Windows users hit immediately (OneDrive sync, Controlled
Folder Access) rather than treating them as edge cases.
"""

from __future__ import annotations

import enum
import functools
import os
import tempfile
from pathlib import Path
from typing import Optional


class VolumeKind(enum.Enum):
    NTFS_LOCAL = "ntfs_local"
    REFS_LOCAL = "refs_local"
    FAT_EXFAT = "fat_exfat"
    SMB_NETWORK = "smb_network"
    UNKNOWN = "unknown"


_FAT_FS_NAMES = {"FAT", "FAT32", "EXFAT"}


def _volume_path_name(path: Path) -> str:
    import win32file  # local import: Windows-only dependency

    return win32file.GetVolumePathName(str(path))


@functools.lru_cache(maxsize=None)
def classify_volume(volume_root: str) -> VolumeKind:
    """Classify the volume rooted at ``volume_root``.

    Cached per volume-root string for the process lifetime: cheap calls, but
    no reason to repeat them for every path component touched in a run.

    Never raises: any Win32 call failure here falls back to UNKNOWN (which
    paths.capability_for treats as UNSAFE_REFUSED, the fail-closed choice),
    but the failure is printed to stderr first -- silently swallowing it
    previously meant a real user (or the first Windows CI run, which is how
    this diagnostic got added) saw a generic "cannot host vault writes
    safely" with no indication of which Win32 call actually failed or why.
    """

    import sys

    import win32file

    try:
        drive_type = win32file.GetDriveType(volume_root)
    except Exception as exc:
        print(
            f"codex-brain: cannot classify volume {volume_root!r} "
            f"(GetDriveType failed): {exc!r}",
            file=sys.stderr,
        )
        return VolumeKind.UNKNOWN
    if drive_type == win32file.DRIVE_REMOTE:
        return VolumeKind.SMB_NETWORK
    try:
        # GetVolumeInformation is exposed via win32api in pywin32, not
        # win32file -- confirmed by the second real Windows CI run, which
        # hit AttributeError: module 'win32file' has no attribute
        # 'GetVolumeInformation' the first time this code ever ran against
        # real pywin32.
        import win32api

        _, _, _, _, fs_name = win32api.GetVolumeInformation(volume_root)
    except Exception as exc:
        print(
            f"codex-brain: cannot classify volume {volume_root!r} "
            f"(GetVolumeInformation failed): {exc!r}",
            file=sys.stderr,
        )
        return VolumeKind.UNKNOWN
    fs_name = (fs_name or "").upper()
    if fs_name in _FAT_FS_NAMES:
        return VolumeKind.FAT_EXFAT
    if fs_name == "REFS":
        return VolumeKind.REFS_LOCAL
    if fs_name == "NTFS":
        return VolumeKind.NTFS_LOCAL
    print(
        f"codex-brain: volume {volume_root!r} reports unrecognized filesystem "
        f"name {fs_name!r}",
        file=sys.stderr,
    )
    return VolumeKind.UNKNOWN


def classify_path(path: Path) -> VolumeKind:
    return classify_volume(_volume_path_name(path))


# OneDrive sets these itself for its sync client; checking them beats
# hardcoding a folder display name, which is locale-specific.
_ONEDRIVE_ENV_VARS = ("OneDriveCommercial", "OneDriveConsumer", "OneDrive")


def detect_onedrive_root(path: Path) -> Optional[Path]:
    """Return the OneDrive sync root containing ``path``, or ``None``."""

    resolved = path.resolve()
    for var in _ONEDRIVE_ENV_VARS:
        value = os.environ.get(var)
        if not value:
            continue
        candidate = Path(value).resolve()
        try:
            resolved.relative_to(candidate)
        except ValueError:
            continue
        return candidate
    return None


_hardlink_support_cache: dict[str, bool] = {}


def supports_hardlinks(volume_root: str) -> bool:
    """Probe once per volume whether hardlinks work there.

    ReFS hardlink support has varied by Windows build; probing beats
    hardcoding a version table that will inevitably go stale.
    """

    if volume_root in _hardlink_support_cache:
        return _hardlink_support_cache[volume_root]
    supported = True
    try:
        with tempfile.TemporaryDirectory(dir=volume_root) as tmp:
            src = os.path.join(tmp, "hardlink-probe-src")
            dst = os.path.join(tmp, "hardlink-probe-dst")
            with open(src, "wb"):
                pass
            os.link(src, dst)
    except OSError:
        supported = False
    _hardlink_support_cache[volume_root] = supported
    return supported
