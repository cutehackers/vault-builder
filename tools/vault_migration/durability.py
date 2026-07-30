"""Small filesystem durability primitives shared by migration writers."""
from __future__ import annotations

import errno
import os
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
