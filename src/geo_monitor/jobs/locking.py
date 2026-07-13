"""Exclusive, stale-aware job bundle locking."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..filesystem import UnsafeOutputPathError, ensure_private_directory
from ..schemas import utc_now_iso
from .contracts import BUNDLE_LOCK, JobError


class JobBundleLock:
    def __init__(self, path: Path, *, timeout_seconds: float = 0.0, stale_seconds: float = 7 * 24 * 60 * 60):
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.stale_seconds = stale_seconds
        self.fd: int | None = None
        self.token: str | None = None

    def __enter__(self) -> "JobBundleLock":
        try:
            ensure_private_directory(self.path.parent)
        except UnsafeOutputPathError as exc:
            raise JobError(str(exc)) from exc
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self.fd = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                self.token = f"{os.getpid()}-{time.time_ns()}"
                os.write(self.fd, json.dumps({"pid": os.getpid(), "token": self.token, "created_at": utc_now_iso()}).encode("utf-8"))
                return self
            except FileExistsError:
                stale_stat = self._stale_stat()
                if stale_stat is not None and self._unlink_if_same(stale_stat):
                    continue
                if time.monotonic() >= deadline:
                    raise JobError(f"任务正在运行，请稍后重试：{self.path}")
                time.sleep(0.05)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
        if not self._owns_lock():
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _stale_stat(self) -> os.stat_result | None:
        try:
            stat = self.path.stat()
            data = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
            if pid > 0 and pid_exists(pid):
                return None
            if pid > 0:
                return stat
            return stat if time.time() - stat.st_mtime > self.stale_seconds else None
        except FileNotFoundError:
            return None
        except Exception:
            try:
                stat = self.path.stat()
                return stat if time.time() - stat.st_mtime > self.stale_seconds else None
            except FileNotFoundError:
                return None

    def _unlink_if_same(self, stale_stat: os.stat_result) -> bool:
        try:
            current = self.path.stat()
        except FileNotFoundError:
            return True
        if (current.st_dev, current.st_ino, current.st_mtime_ns) != (stale_stat.st_dev, stale_stat.st_ino, stale_stat.st_mtime_ns):
            return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return True

    def _owns_lock(self) -> bool:
        if not self.token:
            return False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return False
        return data.get("token") == self.token


def job_bundle_lock(bundle_dir: str | Path) -> JobBundleLock:
    return JobBundleLock(Path(bundle_dir) / BUNDLE_LOCK)


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
