from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "geo_monitor"


def test_internal_domains_do_not_import_the_public_job_facade() -> None:
    internal_files = [
        *SOURCE_ROOT.joinpath("analysis").rglob("*.py"),
        *SOURCE_ROOT.joinpath("duckdb_store").rglob("*.py"),
        *SOURCE_ROOT.joinpath("jobs").rglob("*.py"),
    ]

    offenders = [path for path in internal_files if "from ..job import" in path.read_text(encoding="utf-8")]

    assert offenders == []


def test_removed_compatibility_modules_are_absent() -> None:
    removed = ["dashboard.py", "job_analysis.py", "llm_client.py", "tool.py", "analysis/pipeline.py"]

    assert [name for name in removed if SOURCE_ROOT.joinpath(name).exists()] == []
