"""Reserved for raw ``ctypes``/``ntdll`` calls, deliberately not pre-built.

Per the approved port plan, the ``COMPATIBLE`` tier design (full-path
``CreateFileW`` + immediate ``FileIdInfo`` verification, see
``windows_backend.py``) is the v1 choice specifically to avoid needing
``NtCreateFile`` with a ``RootDirectory`` handle — the one primitive with no
``pywin32`` wrapper and no high-level equivalent. This module exists as the
named landing spot *if* implementation or the Windows CI job (see
``docs/windows-wsl.md``) turns up a real case ``pywin32`` can't cover — e.g.
``FILE_ID_INFO`` being unavailable on a pre-Windows-8/Server-2012 target via
``GetFileInformationByHandleEx``, which would need a raw
``NtQueryInformationFile`` call instead.

Do not add code here speculatively. Every function added must be justified by
a concrete gap hit during implementation or CI, not anticipated in advance.
"""

from __future__ import annotations
