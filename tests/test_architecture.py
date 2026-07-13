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


def test_adapters_do_not_import_provider_sdks() -> None:
    forbidden = ("from openai import", "import dashscope", "volcenginesdkarkruntime")
    offenders = []
    for path in SOURCE_ROOT.joinpath("adapters").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(token in text for token in forbidden):
            offenders.append(path.name)

    assert offenders == []


def test_removed_qwen_compatible_transports_are_absent() -> None:
    assert not SOURCE_ROOT.joinpath("adapters/qwen_chat.py").exists()
    assert not SOURCE_ROOT.joinpath("adapters/qwen_responses.py").exists()
    assert SOURCE_ROOT.joinpath("providers/qwen.py").is_file()
    assert SOURCE_ROOT.joinpath("providers/doubao.py").is_file()
    assert SOURCE_ROOT.joinpath("providers/deepseek.py").is_file()
