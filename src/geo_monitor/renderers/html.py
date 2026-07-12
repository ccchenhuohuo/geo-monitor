"""Optional static HTML renderer; no backend or application framework."""

from __future__ import annotations

import html
import json

from ..report_model import ReportModel


def render_html(model: ReportModel) -> str:
    sections = []
    for section in model.sections:
        blocks = []
        for block in section.blocks:
            if block.kind == "paragraph":
                blocks.append(f"<p>{_text(block.text)}</p>")
            elif block.kind == "bullets":
                blocks.append("<ul>" + "".join(f"<li>{_text(item)}</li>" for item in block.items) + "</ul>")
            elif block.kind == "table" and block.table is not None:
                header = "".join(f"<th>{_text(value)}</th>" for value in block.table.headers)
                rows = "".join("<tr>" + "".join(f"<td>{_text(value)}</td>" for value in row) + "</tr>" for row in block.table.rows)
                note = f'<p class="note">{_text(block.table.note)}</p>' if block.table.note else ""
                blocks.append(f'<div class="table-wrap"><table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table></div>{note}')
        sections.append(f'<section id="{html.escape(section.key, quote=True)}"><h2>{_text(section.title)}</h2>{"".join(blocks)}</section>')
    embedded = (
        json.dumps(model.to_dict(), ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_text(model.title)}</title>
<style>
body {{
  font-family: -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
  color:#172033; max-width:1100px; margin:32px auto; padding:0 24px; line-height:1.65;
}}
h1,h2 {{ color:#0f172a; }} section {{ margin:32px 0; }}
.table-wrap {{ overflow-x:auto; }} table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th,td {{ border:1px solid #d8dee9; padding:7px 9px; text-align:left; vertical-align:top; }} th {{ background:#f4f6f8; }}
.note {{ color:#52606d; font-size:13px; }}
</style>
</head>
<body><h1>{_text(model.title)}</h1>{"".join(sections)}
<script type="application/json" id="report-model">{embedded}</script>
</body></html>
"""


def _text(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)
