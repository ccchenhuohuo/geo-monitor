"""Work-directory cleanup for completed or interrupted job bundles."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ..schemas import utc_now_iso
from .contracts import BUNDLE_LOCK, CLEANUP_SUMMARY
from .layout import work_dir
from .locking import JobBundleLock
from .manifest import load_job_manifest, update_job_manifest, write_json


def cleanup_job_bundle(bundle_dir: str | Path) -> dict[str, Any]:
    root = Path(bundle_dir)
    with JobBundleLock(root / BUNDLE_LOCK):
        return cleanup_job_work_dir_unlocked(root)


def cleanup_job_work_dir_unlocked(bundle_dir: str | Path) -> dict[str, Any]:
    root = Path(bundle_dir)
    manifest = load_job_manifest(root)
    previous_status = str(manifest.get("status") or "")
    path = work_dir(root)
    removed = path.exists()
    if removed:
        shutil.rmtree(path)
    summary = {
        "job_id": manifest.get("job_id"),
        "previous_status": previous_status,
        "removed_work_dir": removed,
        "work_dir": str(path),
        "completed_at": utc_now_iso(),
    }
    write_json(root / CLEANUP_SUMMARY, summary)
    next_status = f"{previous_status}_cleaned" if previous_status in {"analyzed", "analyzed_partial"} else "cleaned"
    update_job_manifest(root, status=next_status)
    return {"bundle_dir": str(root), **summary}
