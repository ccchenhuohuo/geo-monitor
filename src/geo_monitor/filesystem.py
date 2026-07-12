"""Small, shared filesystem safety primitives for generated artifacts."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TextIO

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class UnsafeOutputPathError(ValueError):
    pass


def ensure_private_directory(path: str | Path) -> Path:
    directory = Path(path)
    if directory.is_symlink():
        raise UnsafeOutputPathError(f"拒绝写入 symlink 目录：{directory}")
    directory.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    if not directory.is_dir() or directory.is_symlink():
        raise UnsafeOutputPathError(f"输出目录不是可信普通目录：{directory}")
    _chmod(directory, PRIVATE_DIR_MODE)
    return directory


def prepare_private_output(path: str | Path) -> Path:
    output = Path(path)
    ensure_private_directory(output.parent)
    if output.is_symlink():
        raise UnsafeOutputPathError(f"拒绝覆盖 symlink 文件：{output}")
    if output.exists() and not output.is_file():
        raise UnsafeOutputPathError(f"输出路径不是普通文件：{output}")
    return output


def secure_private_file(path: str | Path) -> None:
    output = Path(path)
    if output.is_symlink() or not output.is_file():
        raise UnsafeOutputPathError(f"无法保护非普通输出文件：{output}")
    _chmod(output, PRIVATE_FILE_MODE)


def open_private_text(
    path: str | Path,
    *,
    append: bool = False,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> TextIO:
    """Open a private regular text file without following a final symlink."""

    output = prepare_private_output(path)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(output, flags, PRIVATE_FILE_MODE)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafeOutputPathError(f"输出文件不是普通文件：{output}")
        try:
            os.fchmod(fd, PRIVATE_FILE_MODE)
        except (AttributeError, OSError):
            if os.name != "nt":
                raise
        return os.fdopen(fd, "a" if append else "w", encoding=encoding, newline=newline)
    except Exception:
        os.close(fd)
        raise


def is_private_file(path: str | Path) -> bool:
    output = Path(path)
    return output.is_file() and not output.is_symlink() and stat.S_IMODE(output.stat().st_mode) & 0o077 == 0


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode, follow_symlinks=False)
    except (NotImplementedError, OSError):
        # Some non-POSIX filesystems do not implement chmod semantics. Path type
        # and symlink checks above remain mandatory there.
        if path.is_symlink():
            raise UnsafeOutputPathError(f"拒绝保护 symlink 路径：{path}")
        if os.name != "nt":
            raise
