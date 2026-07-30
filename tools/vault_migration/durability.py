"""Small filesystem durability primitives shared by migration writers."""
from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path


_UNSUPPORTED_DIRECTORY_FSYNC_ERRORS = frozenset(
    error
    for error in (
        errno.EINVAL,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if error is not None
)


def fsync_directory(path: Path) -> None:
    """Persist directory entries, suppressing only explicit unsupported errors."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRORS:
                raise
    finally:
        os.close(descriptor)


def exchange_directory_entries(
    source_parent: int,
    source_name: str,
    destination_parent: int,
    destination_name: str,
) -> None:
    """Atomically exchange two existing entries without path re-resolution."""
    _rename_with_flags(
        source_parent,
        source_name,
        destination_parent,
        destination_name,
        darwin_flag=0x00000002,
        linux_flag=0x00000002,
        unsupported_message=(
            "Atomic migration replacement requires native file-exchange support."
        ),
        display_operator="<->",
    )


def move_directory_entry_no_replace(
    source_parent: int,
    source_name: str,
    destination_parent: int,
    destination_name: str,
) -> None:
    """Move one entry only if the destination name is still absent."""
    _rename_with_flags(
        source_parent,
        source_name,
        destination_parent,
        destination_name,
        darwin_flag=0x00000004,
        linux_flag=0x00000001,
        unsupported_message=(
            "Recoverable migration deletion requires native no-replace rename support."
        ),
        display_operator="->",
    )


def _rename_with_flags(
    source_parent: int,
    source_name: str,
    destination_parent: int,
    destination_name: str,
    *,
    darwin_flag: int,
    linux_flag: int,
    unsupported_message: str,
    display_operator: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    operation = getattr(
        libc,
        "renameatx_np" if sys.platform == "darwin" else "renameat2",
        None,
    )
    if operation is None:
        raise RuntimeError(unsupported_message)
    operation.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    operation.restype = ctypes.c_int
    flag = darwin_flag if sys.platform == "darwin" else linux_flag
    if (
        operation(
            source_parent,
            os.fsencode(source_name),
            destination_parent,
            os.fsencode(destination_name),
            flag,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(
            error,
            os.strerror(error),
            f"{source_name} {display_operator} {destination_name}",
        )
