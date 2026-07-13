from importlib.resources import files
from pathlib import Path


def test_source_only_resources_are_present_and_nonempty():
    resources = [
        Path("data/job_config.schema.json"),
        Path("examples/job_config.example.json"),
        Path("examples/persona_templates.example.yaml"),
        Path("examples/seed_prompts.example.yaml"),
        Path("docs/intelligence.md"),
        Path("docs/metrics.md"),
        Path("docs/providers.md"),
    ]

    assert all(path.is_file() and path.read_text(encoding="utf-8").strip() for path in resources)


def test_wheel_package_has_no_duplicated_docs_examples_or_schema():
    root = files("geo_monitor")

    assert not any(root.joinpath(name).is_dir() for name in ("data", "docs", "examples"))


def test_intelligence_subpackage_is_part_of_the_distribution():
    root = files("geo_monitor.analysis.intelligence")

    expected = {
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
    assert expected <= {item.name for item in root.iterdir() if item.is_file()}


def test_report_renderers_are_part_of_the_distribution():
    root = files("geo_monitor.renderers")

    assert {"__init__.py", "html.py", "markdown.py", "pdf.py"} <= {
        item.name for item in root.iterdir() if item.is_file()
    }


def test_job_domain_modules_are_part_of_the_distribution():
    root = files("geo_monitor.jobs")

    assert {
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
    } <= {
        item.name for item in root.iterdir() if item.is_file()
    }


def test_duckdb_store_modules_are_part_of_the_distribution():
    root = files("geo_monitor.duckdb_store")

    assert {"__init__.py", "attempts.py", "contracts.py", "ingest.py", "query.py", "results.py", "schema.py"} <= {
        item.name for item in root.iterdir() if item.is_file()
    }
