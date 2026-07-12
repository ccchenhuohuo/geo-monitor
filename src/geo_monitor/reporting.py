from __future__ import annotations

import html
import subprocess
from pathlib import Path
from typing import Any


def build_html(markdown: str, summary: dict[str, Any]) -> str:
    body = markdown_to_html(markdown)
    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"utf-8\">
<title>{html.escape(summary.get("title", "GEO 深入洞察报告"))}</title>
<style>
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
  line-height: 1.65; color: #111827; max-width: 1100px; margin: 36px auto; padding: 0 28px;
}}
h1, h2 {{ color: #0f172a; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0 24px; font-size: 13px; }}
th, td {{ border: 1px solid #d1d5db; padding: 6px 8px; vertical-align: top; }}
th {{ background: #f3f4f6; }}
blockquote {{ border-left: 4px solid #94a3b8; padding-left: 12px; color: #475569; }}
code {{ background: #f1f5f9; padding: 1px 4px; border-radius: 3px; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def markdown_to_html(markdown: str) -> str:
    lines = markdown.splitlines()
    html_lines = []
    in_table = False
    table_rows = []

    def flush_table() -> None:
        nonlocal in_table, table_rows
        if not in_table:
            return
        html_lines.append("<table>")
        for idx, row in enumerate(table_rows):
            cells = [c.strip() for c in row.strip("|").split("|")]
            if idx == 1 and all(set(c) <= {"-", ":", " "} for c in cells):
                continue
            tag = "th" if idx == 0 else "td"
            html_lines.append("<tr>" + "".join(f"<{tag}>" + inline(c) + f"</{tag}>" for c in cells) + "</tr>")
        html_lines.append("</table>")
        in_table = False
        table_rows = []

    for line in lines:
        if line.startswith("|") and line.endswith("|"):
            in_table = True
            table_rows.append(line)
            continue
        flush_table()
        if line.startswith("# "):
            html_lines.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("## "):
            html_lines.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("### "):
            html_lines.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("- "):
            html_lines.append(f"<p>• {inline(line[2:])}</p>")
        elif line.startswith("> "):
            html_lines.append(f"<blockquote>{inline(line[2:])}</blockquote>")
        elif not line.strip():
            html_lines.append("")
        else:
            html_lines.append(f"<p>{inline(line)}</p>")
    flush_table()
    return "\n".join(html_lines)


def inline(text: str) -> str:
    escaped = _escape_html_once(text)
    escaped = re_bold(escaped)
    return escaped.replace("`", "")


def markdown_text(value: object) -> str:
    text = "" if value is None else str(value)
    return html.escape(text, quote=False)


def table_cell(value: object) -> str:
    return markdown_text(value).replace("|", "；").replace("\n", " ")


def re_bold(text: str) -> str:
    import re

    return re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", text)


def try_generate_pdf(html_path: Path, pdf_path: Path) -> bool:
    try:
        subprocess.run(["textutil", "-convert", "pdf", str(html_path), "-output", str(pdf_path)], check=True, capture_output=True, text=True, timeout=120)
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return True
    except Exception:
        pass
    try:
        markdown_path = html_path.with_suffix(".md")
        generate_pdf_with_reportlab(markdown_path, pdf_path)
        return pdf_path.exists() and pdf_path.stat().st_size > 0
    except Exception:
        return False


def generate_pdf_with_reportlab(markdown_path: Path, pdf_path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    styles = getSampleStyleSheet()
    for style_name in ["Normal", "Title", "Heading1", "Heading2", "Heading3"]:
        styles[style_name].fontName = "STSong-Light"
        styles[style_name].leading = max(styles[style_name].leading, 15)
    styles["Normal"].fontSize = 9
    styles["Heading1"].fontSize = 16
    styles["Heading2"].fontSize = 13

    doc = SimpleDocTemplate(str(pdf_path), pagesize=A4, rightMargin=1.4 * cm, leftMargin=1.4 * cm, topMargin=1.4 * cm, bottomMargin=1.4 * cm)
    story = []
    lines = markdown_path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            story.append(Spacer(1, 0.12 * cm))
            i += 1
            continue
        if line.startswith("# "):
            story.append(Paragraph(_clean_md_inline(line[2:]), styles["Heading1"]))
        elif line.startswith("## "):
            story.append(Paragraph(_clean_md_inline(line[3:]), styles["Heading2"]))
        elif line.startswith("### "):
            story.append(Paragraph(_clean_md_inline(line[4:]), styles["Heading3"]))
        elif line.startswith("|") and line.endswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].startswith("|") and lines[i].endswith("|"):
                table_lines.append(lines[i])
                i += 1
            data = []
            for idx, tline in enumerate(table_lines):
                cells = [Paragraph(_clean_md_inline(c.strip())[:450], styles["Normal"]) for c in tline.strip("|").split("|")]
                if idx == 1 and all(set(str(c.getPlainText())) <= {"-", ":", " "} for c in cells):
                    continue
                data.append(cells)
            if data:
                table = Table(data, repeatRows=1)
                table.setStyle(
                    TableStyle(
                        [
                            ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
                            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("FONTSIZE", (0, 0), (-1, -1), 7),
                            ("LEFTPADDING", (0, 0), (-1, -1), 3),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                        ]
                    )
                )
                story.append(table)
            continue
        elif line.startswith("- "):
            story.append(Paragraph("• " + _clean_md_inline(line[2:]), styles["Normal"]))
        elif line.startswith("> "):
            story.append(Paragraph(_clean_md_inline(line[2:]), styles["Normal"]))
        else:
            story.append(Paragraph(_clean_md_inline(line), styles["Normal"]))
        i += 1
    doc.build(story)


def _clean_md_inline(text: str) -> str:
    import re

    text = _escape_html_once(text)
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)
    text = text.replace("`", "")
    return text


def _escape_html_once(text: str) -> str:
    # Dynamic report values are escaped in Markdown so the Markdown artifact is
    # safe on its own. Normalize those entities before escaping for HTML to
    # avoid rendering them as literal ``&lt;...&gt;`` text.
    return html.escape(html.unescape(str(text)))
