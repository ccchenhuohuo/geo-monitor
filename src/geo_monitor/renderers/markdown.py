"""Deterministic Markdown renderer for ReportModel."""

from __future__ import annotations

import re

from ..report_model import ReportModel, ReportTable

_MARKDOWN_CONTROL = re.compile(r"([\\`*_{}\[\]#+.!|>\-])")


def render_markdown(model: ReportModel) -> str:
    lines = [f"# {markdown_text(model.title)}", ""]
    for index, section in enumerate(model.sections, start=1):
        lines.extend([f"## {index}. {markdown_text(section.title)}", ""])
        for block in section.blocks:
            if block.kind == "paragraph":
                if block.text:
                    lines.extend([markdown_text(block.text), ""])
            elif block.kind == "bullets":
                lines.extend(f"- {markdown_text(item)}" for item in block.items)
                lines.append("")
            elif block.kind == "table" and block.table is not None:
                lines.extend(_render_table(block.table))
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_table(table: ReportTable) -> list[str]:
    lines = ["| " + " | ".join(table_cell(value) for value in table.headers) + " |"]
    lines.append("|" + "|".join("---" for _ in table.headers) + "|")
    lines.extend("| " + " | ".join(table_cell(value) for value in row) + " |" for row in table.rows)
    if table.note:
        lines.extend(["", f"> {markdown_text(table.note)}"])
    return lines


def markdown_text(value: object) -> str:
    text = "" if value is None else str(value)
    escaped_html = text.replace("<", "&lt;").replace(">", "&gt;")
    return _MARKDOWN_CONTROL.sub(r"\\\1", escaped_html)


def table_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return markdown_text(text.replace("|", "；").replace("\n", " "))
