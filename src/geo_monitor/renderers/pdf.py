"""Deterministic PDF renderer for ReportModel."""

from __future__ import annotations

from pathlib import Path

from ..report_model import ReportModel


class PdfRenderError(RuntimeError):
    pass


def render_pdf(model: ReportModel, output_path: str | Path) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, TableStyle
    except ImportError as exc:  # pragma: no cover - packaging contract guards this
        raise PdfRenderError("缺少 PDF renderer 依赖 reportlab；请重新安装 geo-monitor") from exc

    output = Path(output_path)
    wide = any(block.table is not None and len(block.table.headers) > 5 for section in model.sections for block in section.blocks)
    pagesize = landscape(A4) if wide else A4
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    styles = getSampleStyleSheet()
    normal = ParagraphStyle("GeoNormal", parent=styles["Normal"], fontName="STSong-Light", fontSize=9, leading=14, alignment=TA_LEFT)
    heading1 = ParagraphStyle("GeoTitle", parent=styles["Title"], fontName="STSong-Light", fontSize=18, leading=24)
    heading2 = ParagraphStyle("GeoHeading", parent=styles["Heading2"], fontName="STSong-Light", fontSize=13, leading=18, spaceBefore=8)
    doc = SimpleDocTemplate(
        str(output),
        pagesize=pagesize,
        rightMargin=1.2 * cm,
        leftMargin=1.2 * cm,
        topMargin=1.2 * cm,
        bottomMargin=1.2 * cm,
        title=model.title,
        author="geo-monitor",
    )
    story = [Paragraph(_xml(model.title), heading1), Spacer(1, 0.3 * cm)]
    for index, section in enumerate(model.sections, start=1):
        if index > 1 and section.key == "methodology":
            story.append(PageBreak())
        story.append(Paragraph(f"{index}. {_xml(section.title)}", heading2))
        for block in section.blocks:
            if block.kind == "paragraph" and block.text:
                story.extend([Paragraph(_xml(block.text), normal), Spacer(1, 0.12 * cm)])
            elif block.kind == "bullets":
                for item in block.items:
                    story.append(Paragraph("• " + _xml(item), normal))
                story.append(Spacer(1, 0.12 * cm))
            elif block.kind == "table" and block.table is not None:
                data = [[Paragraph(_xml(value), normal) for value in block.table.headers]]
                data.extend([Paragraph(_xml(value), normal) for value in row] for row in block.table.rows)
                if data:
                    table = LongTable(data, repeatRows=1, hAlign="LEFT")
                    table.setStyle(
                        TableStyle(
                            [
                                ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
                                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f6")),
                                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                                ("TOPPADDING", (0, 0), (-1, -1), 3),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                            ]
                        )
                    )
                    story.extend([table, Spacer(1, 0.18 * cm)])
                if block.table.note:
                    story.extend([Paragraph(_xml(block.table.note), normal), Spacer(1, 0.12 * cm)])
    try:
        doc.build(story)
    except Exception as exc:  # noqa: BLE001
        raise PdfRenderError(f"PDF 渲染失败：{exc}") from exc
    if not output.is_file() or output.stat().st_size < 1_000:
        raise PdfRenderError("PDF 渲染未产生有效文件")


def _xml(value: object) -> str:
    import html

    return html.escape("" if value is None else str(value), quote=False).replace("\n", "<br/>")
