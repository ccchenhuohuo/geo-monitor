"""Security checks shared by persisted job-bundle readers."""

from __future__ import annotations

from pathlib import Path

from .contracts import JobError


def ensure_bundle_regular_file(root: Path, path: Path, label: str) -> None:
    """Reject links, non-files, and paths that escape the job bundle."""

    if path.is_symlink() or not path.is_file():
        raise JobError(f"bundle {label} 必须是普通非 symlink 文件：{path}")
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise JobError(f"bundle {label} 逃逸任务目录：{path}") from exc
