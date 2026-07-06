import json
from importlib.resources import files
from pathlib import Path


def test_packaged_schema_and_top_level_schema_match():
    packaged = files("geo_monitor").joinpath("data/job_config.schema.json").read_text(encoding="utf-8")
    top_level = Path("data/job_config.schema.json").read_text(encoding="utf-8")

    assert json.loads(packaged)["title"] == "GEO Monitor Job Config"
    assert json.loads(packaged) == json.loads(top_level)


def test_packaged_examples_are_available():
    root = files("geo_monitor")

    assert root.joinpath("examples/job_config.example.json").is_file()
    assert root.joinpath("examples/seed_prompts.example.yaml").is_file()
    packaged_registry = root.joinpath("examples/persona_templates.example.yaml").read_text(encoding="utf-8")
    top_level_registry = Path("examples/persona_templates.example.yaml").read_text(encoding="utf-8")
    assert packaged_registry == top_level_registry
    assert "persona-template-registry-v1" in packaged_registry


def test_packaged_docs_are_available():
    root = files("geo_monitor")

    assert "Metrics Reference" in root.joinpath("docs/metrics.md").read_text(encoding="utf-8")
    assert root.joinpath("docs/README.zh-CN.md").read_text(encoding="utf-8") == Path("README.zh-CN.md").read_text(encoding="utf-8")
