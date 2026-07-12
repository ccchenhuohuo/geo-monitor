"""Stable facade for report-model construction and artifact rendering."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Iterable

from .filesystem import ensure_private_directory, open_private_text, secure_private_file
from .renderers import markdown_text, render_html, render_markdown, render_pdf, table_cell
from .renderers.pdf import PdfRenderError
from .report_builder import build_report_model
from .report_model import ReportModel

__all__ = [
    "ReportRenderError",
    "build_job_markdown",
    "markdown_text",
    "normalize_report_formats",
    "render_report_bundle",
    "table_cell",
]


REPORT_FORMATS = {"markdown", "pdf", "html"}


class ReportRenderError(RuntimeError):
    pass


def normalize_report_formats(values: Iterable[str] | None) -> tuple[str, ...]:
    source = ("markdown", "pdf") if values is None else values
    requested = tuple(dict.fromkeys(str(value).strip().lower() for value in source))
    if not requested:
        raise ReportRenderError("report_formats 不能为空")
    unknown = sorted(set(requested) - REPORT_FORMATS)
    if unknown:
        raise ReportRenderError(f"不支持的报告格式：{', '.join(unknown)}")
    if "pdf" in requested and "markdown" not in requested:
        requested = ("markdown", *requested)
    return requested


def render_report_bundle(
    summary: dict,
    report_dir: str | Path,
    *,
    formats: Iterable[str] | None = None,
) -> tuple[ReportModel, dict[str, Path]]:
    """Render report.json plus requested human-readable formats atomically."""

    requested = normalize_report_formats(formats)
    output_dir = ensure_private_directory(report_dir)
    model = build_report_model(summary)
    stage = ensure_private_directory(output_dir / f".report-bundle.{os.getpid()}.{time.time_ns()}.tmp")
    staged: dict[str, Path] = {"model": stage / "report.json"}
    if "markdown" in requested:
        staged["markdown"] = stage / "report.md"
    if "pdf" in requested:
        staged["pdf"] = stage / "report.pdf"
    if "html" in requested:
        staged["html"] = stage / "report.html"
    try:
        _write_text_atomic(
            staged["model"],
            json.dumps(model.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        if "markdown" in staged:
            _write_text_atomic(staged["markdown"], render_markdown(model))
        if "pdf" in staged:
            _render_pdf_atomic(model, staged["pdf"])
        if "html" in staged:
            _write_text_atomic(staged["html"], render_html(model))

        files: dict[str, Path] = {}
        for key, staged_path in staged.items():
            destination = output_dir / staged_path.name
            os.replace(staged_path, destination)
            secure_private_file(destination)
            files[key] = destination
        requested_names = {path.name for path in files.values()}
        for stale_name in {"report.md", "report.pdf", "report.html"} - requested_names:
            (output_dir / stale_name).unlink(missing_ok=True)
        return model, files
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def build_job_markdown(summary: dict) -> str:
    """Backward-compatible pure Markdown entry point."""

    return render_markdown(build_report_model(summary))


def _write_text_atomic(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open_private_text(tmp) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        secure_private_file(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _render_pdf_atomic(model: ReportModel, path: Path) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        try:
            render_pdf(model, tmp)
        except PdfRenderError as exc:
            raise ReportRenderError(str(exc)) from exc
        secure_private_file(tmp)
        os.replace(tmp, path)
        secure_private_file(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
