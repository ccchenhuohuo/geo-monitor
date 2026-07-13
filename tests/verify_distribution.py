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
    "aggregates.py",
    "artifact_commit.py",
    "artifacts.py",
    "brand_metrics.py",
    "cache.py",
    "contracts.py",
    "denominator_facts.py",
    "extraction.py",
    "fact_utils.py",
    "history.py",
    "orchestrator.py",
    "quality.py",
    "source_metrics.py",
}

DUCKDB_MODULES = {
    "__init__.py",
    "attempts.py",
    "contracts.py",
    "ingest.py",
    "query.py",
    "results.py",
    "schema.py",
}

JOB_MODULES = {
    "__init__.py",
    "bundle_files.py",
    "cleanup.py",
    "config.py",
    "contracts.py",
    "layout.py",
    "locking.py",
    "manifest.py",
    "profiles.py",
    "query_manifest.py",
    "runtime.py",
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

SDIST_RESOURCES = {
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "README.zh-CN.md",
    "data/job_config.schema.json",
    "docs/intelligence.md",
    "docs/metrics.md",
    "docs/providers.md",
    "examples/job_config.example.json",
    "examples/persona_templates.example.yaml",
    "examples/seed_prompts.example.yaml",
    "pyproject.toml",
}

FORBIDDEN_PARTS = {"__pycache__", ".env", "attempts.jsonl"}
FORBIDDEN_WHEEL_PATHS = {
    "geo_monitor/analysis/facts.py",
    "geo_monitor/analysis/pipeline.py",
    "geo_monitor/dashboard.py",
    "geo_monitor/job_analysis.py",
    "geo_monitor/llm_client.py",
    "geo_monitor/tool.py",
}


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
        forbidden = sorted(FORBIDDEN_WHEEL_PATHS & names)
        if forbidden:
            raise AssertionError(f"wheel contains removed compatibility modules: {', '.join(forbidden)}")
        duplicated_resources = sorted(name for name in names if name.startswith(("geo_monitor/data/", "geo_monitor/docs/", "geo_monitor/examples/")))
        if duplicated_resources:
            raise AssertionError(f"wheel contains duplicated source resources: {', '.join(duplicated_resources)}")
        missing_report_modules = sorted(REPORT_MODULES - names)
        if missing_report_modules:
            raise AssertionError(f"wheel missing report modules: {', '.join(missing_report_modules)}")

        analysis_prefix = "geo_monitor/analysis/"
        analysis_modules = {
            name.removeprefix(analysis_prefix) for name in names if name.startswith(analysis_prefix) and "/" not in name.removeprefix(analysis_prefix)
        }
        missing_analysis_modules = sorted(ANALYSIS_MODULES - analysis_modules)
        if missing_analysis_modules:
            raise AssertionError(f"wheel missing analysis modules: {', '.join(missing_analysis_modules)}")

        jobs_prefix = "geo_monitor/jobs/"
        job_modules = {name.removeprefix(jobs_prefix) for name in names if name.startswith(jobs_prefix) and name.endswith(".py")}
        missing_job_modules = sorted(JOB_MODULES - job_modules)
        if missing_job_modules:
            raise AssertionError(f"wheel missing job modules: {', '.join(missing_job_modules)}")

        duckdb_prefix = "geo_monitor/duckdb_store/"
        duckdb_modules = {name.removeprefix(duckdb_prefix) for name in names if name.startswith(duckdb_prefix) and name.endswith(".py")}
        missing_duckdb_modules = sorted(DUCKDB_MODULES - duckdb_modules)
        if missing_duckdb_modules:
            raise AssertionError(f"wheel missing DuckDB modules: {', '.join(missing_duckdb_modules)}")

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
        if "Version: 0.2.0" not in metadata:
            raise AssertionError("wheel metadata does not declare version 0.2.0")
        if not re.search(r"^Requires-Dist: openai.*>=1\.66\.0", metadata, re.MULTILINE):
            raise AssertionError("wheel metadata does not retain the OpenAI SDK >=1.66.0 floor")
        if not re.search(r"^Requires-Dist: reportlab.*>=4\.2\.0", metadata, re.MULTILINE | re.IGNORECASE):
            raise AssertionError("wheel metadata does not declare ReportLab as a core dependency")
        duckdb_requirements = [line for line in metadata.splitlines() if line.lower().startswith("requires-dist: duckdb")]
        if len(duckdb_requirements) != 1 or 'extra == "duckdb"' not in duckdb_requirements[0]:
            raise AssertionError("DuckDB must be declared exactly once and only under the duckdb extra")
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
    packaged_tests = sorted(name for name in names if name.startswith("tests/"))
    if packaged_tests:
        raise AssertionError(f"sdist must not contain the repository test suite: {', '.join(packaged_tests)}")
    missing_report_modules = sorted({f"src/{name}" for name in REPORT_MODULES} - names)
    if missing_report_modules:
        raise AssertionError(f"sdist missing report modules: {', '.join(missing_report_modules)}")
    analysis_prefix = "src/geo_monitor/analysis/"
    analysis_modules = {
        name.removeprefix(analysis_prefix) for name in names if name.startswith(analysis_prefix) and "/" not in name.removeprefix(analysis_prefix)
    }
    missing_analysis_modules = sorted(ANALYSIS_MODULES - analysis_modules)
    if missing_analysis_modules:
        raise AssertionError(f"sdist missing analysis modules: {', '.join(missing_analysis_modules)}")
    jobs_prefix = "src/geo_monitor/jobs/"
    job_modules = {name.removeprefix(jobs_prefix) for name in names if name.startswith(jobs_prefix) and name.endswith(".py")}
    missing_job_modules = sorted(JOB_MODULES - job_modules)
    if missing_job_modules:
        raise AssertionError(f"sdist missing job modules: {', '.join(missing_job_modules)}")
    duckdb_prefix = "src/geo_monitor/duckdb_store/"
    duckdb_modules = {name.removeprefix(duckdb_prefix) for name in names if name.startswith(duckdb_prefix) and name.endswith(".py")}
    missing_duckdb_modules = sorted(DUCKDB_MODULES - duckdb_modules)
    if missing_duckdb_modules:
        raise AssertionError(f"sdist missing DuckDB modules: {', '.join(missing_duckdb_modules)}")
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
