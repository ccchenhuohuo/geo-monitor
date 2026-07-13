from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .analysis import analyze_job_bundle, estimate_job_analysis
from .config import Settings, get_settings, live_endpoint_status, redact_url
from .dataset import DatasetError
from .exporters import export_csv, read_jsonl_with_errors
from .fanout import FanoutError, build_query_manifest
from .job import JobError, build_job_bundle, cleanup_job_bundle, estimate_job_run, run_job_bundle, validate_job_config
from .providers import ProviderDependencyError, get_provider, provider_dependency_available

app = typer.Typer(help="支持通用兼容与原生 provider SDK 的 GEO 品牌分析与报告引擎")
db_app = typer.Typer(help="DuckDB 轻量分析层")
app.add_typer(db_app, name="db")
console = Console()


def _parse_ids(value: str | None) -> list[str] | None:
    if value is None:
        return None
    values = [item.strip() for item in value.split(",") if item.strip()]
    if not values:
        raise typer.BadParameter("--only-query-id 已提供但没有有效 ID；拒绝回退为全量执行")
    return values


@app.command()
def doctor() -> None:
    settings = get_settings()
    table = Table(title="GEO Monitor 配置检查")
    table.add_column("配置")
    table.add_column("值")
    for key, value in settings.redacted().items():
        table.add_row(key, str(value))
    console.print(table)
    native_key_configured = any(settings.provider_api_key_status(name) == "configured" for name in ("doubao", "qwen", "deepseek"))
    if not settings.has_api_key and not native_key_configured:
        console.print("[yellow]未检测到有效 API key；dry-run 和 mock-run 可正常使用，真实调用需要配置 provider key。[/yellow]")
    if settings.llm_base_url_status == "placeholder" and settings.has_api_key:
        console.print("[yellow]通用 provider 的 LLM_BASE_URL 仍是示例值；原生 provider 不受此项影响。[/yellow]")
    elif settings.llm_base_url_status == "invalid":
        console.print("[yellow]LLM_BASE_URL 无效；请配置包含 http(s) scheme 和 host 的 endpoint。[/yellow]")
    elif settings.llm_base_url_status == "insecure":
        console.print("[yellow]LLM_BASE_URL 使用明文 HTTP；live 调用默认会被拒绝。[/yellow]")
    for provider_name in ("doubao", "qwen", "deepseek"):
        try:
            get_provider(provider_name).validate_endpoint(settings)
        except ValueError as exc:
            console.print(f"[yellow]{provider_name} 原生 endpoint 配置无效：{exc}[/yellow]")
        dedicated_key = {
            "doubao": settings.ark_api_key,
            "qwen": settings.dashscope_api_key,
            "deepseek": settings.deepseek_api_key,
        }[provider_name]
        dedicated_key_value = dedicated_key.get_secret_value().strip() if dedicated_key is not None else ""
        if dedicated_key_value and settings.provider_api_key_status(provider_name) == "configured" and not provider_dependency_available(provider_name):
            console.print(f"{provider_name} provider SDK 未安装；请安装 geo-monitor[{provider_name}]。", markup=False)
    if not os.getenv("GEO_MONITOR_ENV_FILE"):
        console.print("[cyan]默认不再读取当前目录 .env；如需使用 .env，请设置 GEO_MONITOR_ENV_FILE=/abs/path/.env。[/cyan]")
    if not os.getenv("GEO_MONITOR_WORKSPACE") and _looks_like_project_root(Path.cwd()):
        console.print(
            "[yellow]当前未设置 GEO_MONITOR_WORKSPACE，且 cwd 看起来是项目仓库根目录；"
            "长期 study 建议使用外部 workspace 或显式 --runs-dir。[/yellow]"
        )


def _looks_like_project_root(path: Path) -> bool:
    return (path / "pyproject.toml").exists() and (path / "src" / "geo_monitor").exists()


def _profile_endpoint(profile: dict | None, settings: Settings) -> tuple[str, str]:
    provider_name = str((profile or {}).get("provider") or "openai_compatible")
    endpoint = get_provider(provider_name).endpoint_url(settings)
    return endpoint, live_endpoint_status(endpoint)


@app.command("export-csv")
def export_csv_command(
    input_jsonl: Annotated[Path, typer.Argument(help="raw/attempts.jsonl 或其他 JSONL 审计文件")],
    out: Annotated[Path, typer.Option("--out", help="CSV 输出路径")],
) -> None:
    records, errors = read_jsonl_with_errors(input_jsonl)
    export_csv(records, out)
    if errors:
        console.print(f"[yellow]已跳过 {len(errors)} 行无法解析的 JSONL 记录。[/yellow]")
    console.print(f"[green]已导出 CSV：{out}（{len(records)} 行）[/green]")


@app.command("build-job")
def build_job_command(
    job_config: Annotated[Path, typer.Argument(help="job_config.json 任务配置")],
    out_dir: Annotated[Path | None, typer.Option("--out-dir", help="显式任务交付目录；与 --runs-dir 二选一")] = None,
    runs_dir: Annotated[Path | None, typer.Option("--runs-dir", help="外部 study workspace 下的 runs 目录")] = None,
    query_manifest: Annotated[Path | None, typer.Option("--query-manifest", help="外部 frozen query manifest CSV")] = None,
    force: bool = typer.Option(False, "--force", help="允许覆盖非空任务目录"),
) -> None:
    try:
        result = build_job_bundle(job_config, out_dir, force=force, query_manifest_path=query_manifest, runs_dir=runs_dir)
    except (DatasetError, JobError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[green]任务已生成：{result['bundle_dir']}[/green]")
    console.print(f"query_manifest: {result['query_manifest']}")


@app.command("fanout")
def fanout_command(
    input_path: Annotated[Path, typer.Option("--input", help="seed_prompts.yaml 输入路径")],
    output_path: Annotated[Path, typer.Option("--output", help="外部 frozen query_manifest.csv 输出路径")],
    persona_template_registry: Annotated[
        Path | None,
        typer.Option("--persona-template-registry", help="显式 external persona template registry YAML"),
    ] = None,
    force: bool = typer.Option(False, "--force", help="允许覆盖已存在 manifest"),
) -> None:
    try:
        result = build_query_manifest(
            input_path,
            output_path,
            force=force,
            persona_template_registry_path=persona_template_registry,
        )
    except FanoutError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[green]fanout 完成：{result['output']}（{result['row_count']} 行）[/green]")


@app.command("validate-job-config")
def validate_job_config_command(
    job_config: Annotated[Path, typer.Argument(help="job_config.json 任务配置")],
    query_manifest: Annotated[Path | None, typer.Option("--query-manifest", help="外部 frozen query manifest CSV；用于校验 external manifest 模式配置")] = None,
) -> None:
    try:
        result = validate_job_config(job_config, query_manifest_path=query_manifest)
    except (DatasetError, JobError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(
        f"[green]任务配置有效：{result['query_count']} 条 query × {result['repeats']} repeats = "
        f"{result['planned_units']} 个采样单元；并发 {result['concurrency']}；web_search_limit {result['web_search_limit']}[/green]"
    )


@app.command("run-job")
def run_job_command(
    bundle_dir: Annotated[Path, typer.Argument(help="build-job 生成的任务目录")],
    resume: bool = typer.Option(True, help="是否断点续跑"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只生成请求，不调用 API"),
    mock: bool = typer.Option(False, "--mock", help="使用模拟响应，不调用 API"),
    limit: int | None = typer.Option(None, help="只运行前 N 条 query，用于小样本 smoke"),
    only_query_id: str | None = typer.Option(None, help="只运行指定 query_id，多个用逗号分隔"),
    query_manifest: Path | None = typer.Option(None, "--query-manifest", help="work/query_manifest.csv 缺失或 hash mismatch 时使用的 replacement manifest"),
    sleep_seconds: float = typer.Option(0.0, help="每次调用后等待秒数"),
    start_interval_seconds: float | None = typer.Option(None, help="并发模式下每个请求启动之间的间隔秒数；不传则使用 job_manifest 配置"),
    confirm_cost: bool = typer.Option(False, "--confirm-cost", help="确认执行 live API 请求预算"),
) -> None:
    try:
        only_query_ids = _parse_ids(only_query_id)
        estimate_kwargs = {
            "dry_run": dry_run,
            "mock": mock,
            "resume": resume,
            "limit": limit,
            "only_query_ids": only_query_ids,
        }
        if query_manifest is not None:
            estimate_kwargs["query_manifest_path"] = query_manifest
        estimate = estimate_job_run(bundle_dir, **estimate_kwargs)
        settings = get_settings()
        endpoint, endpoint_status = _profile_endpoint(estimate.get("sampling_profile"), settings)
        console.print(
            "预检："
            f"计划采样单元 {estimate['planned_units']}，已完成 {estimate['completed_units']}，"
            f"本次 live 采样请求预计 {estimate['sampling_requests_remaining']}，"
            f"后续分析 LLM 请求预计 {estimate['analysis_llm_requests_estimate']}，"
            f"模型 {estimate.get('model', 'unknown')}，web_search_limit {estimate.get('web_search_limit', 'unknown')}，"
            f"endpoint {redact_url(endpoint)}（{endpoint_status}），"
            f"并发 {estimate['concurrency']}，启动间隔 {estimate['start_interval_seconds']}s。"
        )
        if not dry_run and not mock and estimate["sampling_requests_remaining"] > 0 and not confirm_cost:
            raise typer.BadParameter(f"真实 live 调用会产生 API 成本；endpoint={redact_url(endpoint)}；请确认预算后添加 --confirm-cost")
        run_kwargs = {
            "resume": resume,
            "dry_run": dry_run,
            "mock": mock,
            "sleep_seconds": sleep_seconds,
            "start_interval_seconds": start_interval_seconds,
            "limit": limit,
            "only_query_ids": only_query_ids,
            "confirm_cost": confirm_cost,
        }
        if query_manifest is not None:
            run_kwargs["query_manifest_path"] = query_manifest
        result = run_job_bundle(bundle_dir, **run_kwargs)
    except (DatasetError, JobError, ProviderDependencyError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not dry_run and not mock and result["errors"]:
        console.print(
            f"[red]任务运行完成但存在 live 错误：执行 {result['executed']} 次；跳过 {result.get('skipped', 0)}；"
            f"完成单元 {result.get('completed_units', 0)}；错误 {result['errors']}；raw={result['raw_jsonl']}[/red]"
        )
        raise typer.Exit(code=1)
    console.print(
        f"[green]任务运行完成：执行 {result['executed']} 次；跳过 {result.get('skipped', 0)}；"
        f"完成单元 {result.get('completed_units', 0)}；错误 {result['errors']}；raw={result['raw_jsonl']}[/green]"
    )


@app.command("analyze-job")
def analyze_job_command(
    bundle_dir: Annotated[Path, typer.Argument(help="build-job 生成的任务目录")],
    keep_work: bool = typer.Option(False, "--keep-work", help="保留 work/ 中间文件用于调试"),
    include_mock: bool = typer.Option(False, "--include-mock", help="允许 mock 样本进入 demo 分析，报告会标注非 live 结论"),
    confirm_cost: bool = typer.Option(False, "--confirm-cost", help="确认执行分析阶段 LLM 抽取请求预算"),
    refresh_extraction_cache: bool = typer.Option(False, "--refresh-extraction-cache", help="忽略已有抽取/归一化缓存并重新执行分析抽取"),
    aggregate: bool = typer.Option(False, "--aggregate/--no-aggregate", help="显式更新跨 Run 聚合；单次报告默认关闭"),
    html_report: bool = typer.Option(False, "--html-report", help="额外生成静态 report.html；Markdown/PDF 始终生成"),
) -> None:
    try:
        estimate = estimate_job_analysis(bundle_dir, include_mock=include_mock, refresh_extraction_cache=refresh_extraction_cache)
        settings = get_settings()
        endpoint, endpoint_status = _profile_endpoint(estimate.get("analysis_profile"), settings)
        console.print(
            "分析预检："
            f"可分析样本 {estimate['analysis_record_count']}，样本模式 {estimate['sample_mode']}，"
            f"分析 LLM 请求预计 {estimate['analysis_llm_requests_estimate']}，"
            f"模型 {estimate.get('model', 'unknown')}，endpoint {redact_url(endpoint)}（{endpoint_status}）。"
        )
        if estimate["analysis_llm_requests_estimate"] > 0 and not confirm_cost:
            raise typer.BadParameter(f"分析阶段会产生 LLM API 成本；endpoint={redact_url(endpoint)}；请确认预算后添加 --confirm-cost")
        result = analyze_job_bundle(
            bundle_dir,
            keep_work=keep_work,
            include_mock=include_mock,
            confirm_cost=confirm_cost,
            refresh_extraction_cache=refresh_extraction_cache,
            write_aggregates=aggregate,
            report_formats=("markdown", "pdf", "html") if html_report else ("markdown", "pdf"),
        )
    except (DatasetError, JobError, ProviderDependencyError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(
        f"[green]分析报告已生成：{result['report_files']['markdown']}；"
        f"PDF：{result['report_files']['pdf']}[/green]"
    )


@app.command("cleanup-job")
def cleanup_job_command(
    bundle_dir: Annotated[Path, typer.Argument(help="build-job 生成的任务目录")],
) -> None:
    try:
        result = cleanup_job_bundle(bundle_dir)
    except (DatasetError, JobError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[green]清理完成：removed_work_dir={result['removed_work_dir']}[/green]")


@db_app.command("build")
def db_build_command(
    runs: Annotated[Path, typer.Option("--runs", help="study workspace 下的 runs 目录")],
    output: Annotated[Path, typer.Option("--output", help="DuckDB 输出路径")],
    query_manifest: Annotated[Path | None, typer.Option("--query-manifest", help="旧 run 缺失 query_meta 时的 fallback manifest")] = None,
) -> None:
    from .db import DuckDBError, build_duckdb

    try:
        result = build_duckdb(runs, output, query_manifest=query_manifest)
    except DuckDBError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[green]DuckDB 已生成：{result['db_path']}[/green]")


@db_app.command("inspect")
def db_inspect_command(
    db: Annotated[Path, typer.Option("--db", help="DuckDB 文件路径")],
) -> None:
    from .db import DuckDBError, inspect_duckdb

    try:
        result = inspect_duckdb(db)
    except DuckDBError as exc:
        raise typer.BadParameter(str(exc)) from exc
    table = Table(title=f"DuckDB: {result['db_path']}")
    table.add_column("table")
    table.add_column("rows", justify="right")
    for row in result["tables"]:
        table.add_row(str(row["table"]), str(row["row_count"]))
    console.print(table)


@db_app.command("query")
def db_query_command(
    db: Annotated[Path, typer.Option("--db", help="DuckDB 文件路径")],
    sql: Annotated[str, typer.Argument(help="SQL 查询")],
) -> None:
    from .db import DuckDBError, query_duckdb

    try:
        columns, rows = query_duckdb(db, sql)
    except DuckDBError as exc:
        raise typer.BadParameter(str(exc)) from exc
    table = Table()
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*["" if value is None else str(value) for value in row])
    console.print(table)


if __name__ == "__main__":
    app()
