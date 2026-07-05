from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any


class DashboardError(ValueError):
    pass


def build_dashboard(db_path: str | Path, out_dir: str | Path) -> dict[str, Any]:
    db = Path(db_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    index = out / "index.html"
    data = _load_dashboard_data(db)
    tmp = index.with_name(f".{index.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(_render_html(data), encoding="utf-8")
        os.replace(tmp, index)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    return {"dashboard_path": str(index), "out_dir": str(out)}


def _load_dashboard_data(db: Path) -> dict[str, Any]:
    if not db.exists():
        raise DashboardError(f"DuckDB 不存在：{db}")
    duckdb = _duckdb()
    con = duckdb.connect(str(db), read_only=True)
    try:
        return {
            "overview": _one(con, "select count(*) queries from queries"),
            "attempts": _one(con, "select count(*) attempts from attempts"),
            "top_brands": _rows(con, "select brand_name_canonical, avg(sov_event_share) sov from brand_summary group by 1 order by sov desc nulls last limit 10"),
            "persona": _rows(con, "select persona, count(distinct query_id) queries from queries group by 1 order by 1"),
            "seed": _rows(con, "select seed_id, count(distinct query_id) queries from queries group by 1 order by 1"),
            "runs": _rows(con, "select job_id, status, sample_count, target_brand, model from runs order by created_at desc nulls last"),
            "quality": _rows(con, "select type, count(*) count from quality_flags group by 1 order by count desc"),
        }
    finally:
        con.close()


def _one(con: Any, sql: str) -> dict[str, Any]:
    result = con.execute(sql)
    columns = [item[0] for item in result.description or []]
    row = result.fetchone()
    return dict(zip(columns, row or []))


def _rows(con: Any, sql: str) -> list[dict[str, Any]]:
    result = con.execute(sql)
    columns = [item[0] for item in result.description or []]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def _render_html(data: dict[str, Any]) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>GEO Monitor Dashboard</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px auto; max-width: 1180px; padding: 0 24px; color: #18212f; }}
h1, h2 {{ margin: 0 0 14px; }}
section {{ border-top: 1px solid #d8dee8; padding: 24px 0; }}
.metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
.metric {{ border: 1px solid #d8dee8; border-radius: 6px; padding: 12px; background: #f8fafc; }}
.value {{ font-size: 28px; font-weight: 650; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
th, td {{ border: 1px solid #d8dee8; padding: 7px 9px; text-align: left; }}
th {{ background: #eef2f7; }}
</style>
</head>
<body>
<h1>GEO Monitor Dashboard</h1>
<section>
<h2>Overview</h2>
<div class="metrics">
<div class="metric"><div>Queries</div><div class="value">{_e(data["overview"].get("queries", 0))}</div></div>
<div class="metric"><div>Attempts</div><div class="value">{_e(data["attempts"].get("attempts", 0))}</div></div>
</div>
</section>
<section><h2>Top Brands</h2>{_table(data["top_brands"])}</section>
<section><h2>Persona</h2>{_table(data["persona"])}</section>
<section><h2>Seed Query</h2>{_table(data["seed"])}</section>
<section><h2>Runs</h2>{_table(data["runs"])}</section>
<section><h2>Quality</h2>{_table(data["quality"])}</section>
</body>
</html>
"""


def _table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No data.</p>"
    columns = list(rows[0].keys())
    head = "".join(f"<th>{_e(column)}</th>" for column in columns)
    body = "".join("<tr>" + "".join(f"<td>{_e(row.get(column))}</td>" for column in columns) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _duckdb() -> Any:
    try:
        import duckdb
    except ModuleNotFoundError as exc:
        raise DashboardError("缺少 duckdb 依赖，请安装项目依赖：pip install -e .") from exc
    return duckdb
