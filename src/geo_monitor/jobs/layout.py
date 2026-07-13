"""Canonical relationships between a job bundle and its study workspace."""

from __future__ import annotations

from pathlib import Path

from .contracts import LOGS_DIR, RAW_ATTEMPTS, RESULT_DIR, WORK_DIR


def raw_attempts_path(bundle_dir: str | Path) -> Path:
    return Path(bundle_dir) / RAW_ATTEMPTS


def work_dir(bundle_dir: str | Path) -> Path:
    return Path(bundle_dir) / WORK_DIR


def result_dir(bundle_dir: str | Path) -> Path:
    return Path(bundle_dir) / RESULT_DIR


def logs_dir(bundle_dir: str | Path) -> Path:
    return Path(bundle_dir) / LOGS_DIR


def runs_root_for_bundle(bundle_dir: str | Path) -> Path:
    """Return the directory that owns the bundle and its sibling runs."""

    return Path(bundle_dir).parent
