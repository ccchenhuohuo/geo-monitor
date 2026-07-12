"""Verify built sdist/wheel contents without importing the source checkout.

CI runs this script after ``python -m build`` and before installing the wheel.
It intentionally uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import re
import tarfile
import zipfile
from pathlib import Path

INTELLIGENCE_MODULES = {
    "__init__.py",
    "citation.py",
    "common.py",
    "competitor.py",
    "opportunities.py",
    "orchestration.py",
    "overview.py",
    "perception.py",
    "recommendation.py",
    "situation.py",
    "trends.py",
}

ANALYSIS_MODULES = {
    "__init__.py",
    "contracts.py",
    "history.py",
    "orchestrator.py",
    "pipeline.py",
}

REPORT_MODULES = {
    "geo_monitor/report_builder.py",
    "geo_monitor/report_model.py",
    "geo_monitor/reporting.py",
    "geo_monitor/renderers/__init__.py",
    "geo_monitor/renderers/html.py",
    "geo_monitor/renderers/markdown.py",
    "geo_monitor/renderers/pdf.py",
}

WHEEL_RESOURCES = {
    "geo_monitor/data/job_config.schema.json",
    "geo_monitor/docs/README.zh-CN.md",
    "geo_monitor/docs/intelligence.md",
    "geo_monitor/docs/metrics.md",
    "geo_monitor/docs/providers.md",
    "geo_monitor/examples/job_config.example.json",
    "geo_monitor/examples/persona_templates.example.yaml",
    "geo_monitor/examples/seed_prompts.example.yaml",
}

SDIST_RESOURCES = {
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "README.zh-CN.md",
    "docs/intelligence.md",
    "docs/metrics.md",
    "docs/providers.md",
    "pyproject.toml",
}

FORBIDDEN_PARTS = {"__pycache__", ".env", "attempts.jsonl"}


def _one(paths: list[Path], label: str) -> Path:
    if len(paths) != 1:
        names = ", ".join(path.name for path in paths) or "none"
        raise AssertionError(f"expected exactly one {label}, found: {names}")
    return paths[0]


def _assert_no_forbidden(names: set[str]) -> None:
    for name in names:
        parts = set(Path(name).parts)
        overlap = parts & FORBIDDEN_PARTS
        if overlap or name.endswith((".pyc", ".pyo")):
            raise AssertionError(f"forbidden generated/private file in distribution: {name}")


def _assert_wheel(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        _assert_no_forbidden(names)
        missing = sorted(WHEEL_RESOURCES - names)
        if missing:
            raise AssertionError(f"wheel missing resources: {', '.join(missing)}")
        missing_report_modules = sorted(REPORT_MODULES - names)
        if missing_report_modules:
            raise AssertionError(f"wheel missing report modules: {', '.join(missing_report_modules)}")

        analysis_prefix = "geo_monitor/analysis/"
        analysis_modules = {
            name.removeprefix(analysis_prefix)
            for name in names
            if name.startswith(analysis_prefix) and "/" not in name.removeprefix(analysis_prefix)
        }
        missing_analysis_modules = sorted(ANALYSIS_MODULES - analysis_modules)
        if missing_analysis_modules:
            raise AssertionError(f"wheel missing analysis modules: {', '.join(missing_analysis_modules)}")

        intelligence_prefix = "geo_monitor/analysis/intelligence/"
        modules = {name.removeprefix(intelligence_prefix) for name in names if name.startswith(intelligence_prefix) and name.endswith(".py")}
        missing_modules = sorted(INTELLIGENCE_MODULES - modules)
        if missing_modules:
            raise AssertionError(f"wheel missing intelligence modules: {', '.join(missing_modules)}")

        metadata_name = _one(
            [Path(name) for name in names if name.endswith(".dist-info/METADATA")],
            "wheel METADATA",
        ).as_posix()
        metadata = archive.read(metadata_name).decode("utf-8")
        if "Requires-Python: >=3.11" not in metadata:
            raise AssertionError("wheel metadata does not declare Python >=3.11")
        if not re.search(r"^Requires-Dist: openai.*>=1\.66\.0", metadata, re.MULTILINE):
            raise AssertionError("wheel metadata does not retain the OpenAI SDK >=1.66.0 floor")
        if not re.search(r"^Requires-Dist: reportlab.*>=4\.2\.0", metadata, re.MULTILINE | re.IGNORECASE):
            raise AssertionError("wheel metadata does not declare ReportLab as a core dependency")
        if not any(name.endswith(".dist-info/licenses/LICENSE") for name in names):
            raise AssertionError("wheel is missing the declared license file")


def _strip_sdist_root(name: str) -> str:
    _, separator, relative = name.partition("/")
    return relative if separator else name


def _assert_sdist(sdist: Path) -> None:
    with tarfile.open(sdist, mode="r:gz") as archive:
        names = {_strip_sdist_root(member.name) for member in archive.getmembers() if member.isfile()}
    _assert_no_forbidden(names)
    missing = sorted(SDIST_RESOURCES - names)
    if missing:
        raise AssertionError(f"sdist missing resources: {', '.join(missing)}")
    missing_report_modules = sorted({f"src/{name}" for name in REPORT_MODULES} - names)
    if missing_report_modules:
        raise AssertionError(f"sdist missing report modules: {', '.join(missing_report_modules)}")
    analysis_prefix = "src/geo_monitor/analysis/"
    analysis_modules = {
        name.removeprefix(analysis_prefix)
        for name in names
        if name.startswith(analysis_prefix) and "/" not in name.removeprefix(analysis_prefix)
    }
    missing_analysis_modules = sorted(ANALYSIS_MODULES - analysis_modules)
    if missing_analysis_modules:
        raise AssertionError(f"sdist missing analysis modules: {', '.join(missing_analysis_modules)}")
    intelligence_prefix = "src/geo_monitor/analysis/intelligence/"
    modules = {name.removeprefix(intelligence_prefix) for name in names if name.startswith(intelligence_prefix) and name.endswith(".py")}
    missing_modules = sorted(INTELLIGENCE_MODULES - modules)
    if missing_modules:
        raise AssertionError(f"sdist missing intelligence modules: {', '.join(missing_modules)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", type=Path)
    args = parser.parse_args()
    dist_dir = args.dist_dir.resolve()
    wheel = _one(sorted(dist_dir.glob("*.whl")), "wheel")
    sdist = _one(sorted(dist_dir.glob("*.tar.gz")), "sdist")
    _assert_wheel(wheel)
    _assert_sdist(sdist)
    print(f"verified {wheel.name} and {sdist.name}")


if __name__ == "__main__":
    main()
