"""Render the post-incident report as a PDF.

The report already exists as Markdown, assembled by `postmortem.markdown()` from the recorded
investigation. This module only typesets it. Nothing here reads the database, asks a model or
decides anything: give it different Markdown and it renders that instead, which is what keeps the
downloaded file and the page in the console the same document.

**Why a renderer rather than a converter.** The Markdown this has to handle is not arbitrary - it
is whatever `postmortem.markdown()` emits, which is a closed set: three heading levels, bold,
italic and code inline, one blockquote, bullet lists, pipe tables and a horizontal rule. A general
Markdown-to-PDF pipeline would pull in a parser and an HTML engine to cover syntax this project
never writes. So the subset is parsed directly, and anything unrecognised falls through as a
paragraph rather than being dropped - an unreadable line is a formatting bug, a missing line in a
postmortem is a lie.

ReportLab's `Paragraph` accepts a small HTML-like markup, which is what the inline conversion
targets. That markup is XML, so text is escaped *before* the markers are turned into tags -
otherwise a log line containing "<" would break the document rather than appear in it.
"""

from __future__ import annotations

import re
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (HRFlowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

# Printed on paper and read on screen, so the palette is ink on white rather than the console's
# dark theme. The accent matches the console's so the two read as one system.
INK = colors.HexColor("#1a1d21")
INK_2 = colors.HexColor("#40464e")
INK_3 = colors.HexColor("#6b7280")
LINE = colors.HexColor("#d7dbe0")
ACCENT = colors.HexColor("#2f6fdb")
SUNK = colors.HexColor("#f5f6f8")


def _styles() -> dict:
    base = getSampleStyleSheet()["BodyText"]

    def style(name: str, **kw) -> ParagraphStyle:
        kw.setdefault("fontName", "Helvetica")
        kw.setdefault("textColor", INK_2)
        kw.setdefault("alignment", TA_LEFT)
        return ParagraphStyle(name, parent=base, **kw)

    return {
        "body": style("body", fontSize=9.5, leading=14.5, spaceAfter=7),
        "h1": style("h1", fontName="Helvetica-Bold", fontSize=18, leading=23,
                    textColor=INK, spaceBefore=0, spaceAfter=3),
        "h2": style("h2", fontName="Helvetica-Bold", fontSize=10.5, leading=14,
                    textColor=INK_3, spaceBefore=16, spaceAfter=6),
        "h3": style("h3", fontName="Helvetica-Bold", fontSize=10.5, leading=14,
                    textColor=INK, spaceBefore=11, spaceAfter=4),
        # The "written by AIRA" note under the title: it qualifies everything below it, so it is
        # set apart rather than left to read as the first sentence of the report.
        "quote": style("quote", fontName="Helvetica-Oblique", fontSize=8.5, leading=12.5,
                       textColor=INK_3, leftIndent=9, spaceBefore=4, spaceAfter=10),
        "meta": style("meta", fontSize=8.8, leading=13, textColor=INK_3, spaceAfter=2),
        "li": style("li", fontSize=9.5, leading=14.5, leftIndent=12, bulletIndent=2, spaceAfter=3),
        "cell": style("cell", fontSize=8.6, leading=12),
        "cellh": style("cellh", fontName="Helvetica-Bold", fontSize=8.6, leading=12,
                       textColor=INK),
        "foot": style("foot", fontName="Helvetica-Oblique", fontSize=8, leading=11,
                      textColor=INK_3, spaceBefore=4),
    }


def _esc(text: str) -> str:
    """XML-escape first: the inline markers below become tags, and the content must not."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def inline(text: str) -> str:
    """Markdown emphasis to ReportLab's inline markup.

    Order matters: `code` is converted last so a backticked span containing an underscore - a
    metric name like `error_ratio_baseline`, which this report is full of - is not first mangled
    into italics.
    """
    out = _esc(text or "")
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    # Underscore italics only between word boundaries, for the same reason: identifiers in this
    # report carry underscores and must survive intact.
    out = re.sub(r"(?<![\w`])_([^_`]+?)_(?![\w`])", r"<i>\1</i>", out)
    out = re.sub(r"`(.+?)`", r'<font face="Courier" size="8.6">\1</font>', out, flags=re.S)
    return out


def _table(rows: list[list[str]], st: dict, width: float) -> Table:
    """A pipe table, sized to the page and wrapped per cell.

    Column widths are proportional to the longest cell in each column rather than equal: the
    timeline's "Stage" column carries sentences next to a "Duration" column of four characters,
    and equal columns wrap the first into a ribbon while the last sits mostly empty.
    """
    header, *body = rows
    cols = len(header)
    weights = []
    for i in range(cols):
        longest = max([len(header[i])] + [len(r[i]) for r in body] or [1])
        weights.append(max(longest, 6))
    total = sum(weights) or 1
    # No column narrower than a tenth of the table, so a short column still fits its header.
    widths = [max(width * (w / total), width * 0.10) for w in weights]
    scale = width / sum(widths)
    widths = [w * scale for w in widths]

    data = [[Paragraph(inline(c), st["cellh"]) for c in header]]
    data += [[Paragraph(inline(c), st["cell"]) for c in r] for r in body]
    t = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SUNK),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE),
        ("LINEBELOW", (0, 1), (-1, -2), 0.3, LINE),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_divider(line: str) -> bool:
    """The `| --- | --- |` line under a table header."""
    cells = _split_row(line)
    return bool(cells) and all(set(c) <= set("-: ") and "-" in c for c in cells)


def flowables(md: str, st: dict, width: float) -> list:
    """Parse the Markdown subset into ReportLab flowables, in order."""
    out: list = []
    lines = (md or "").split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()

        if not line:
            i += 1
            continue

        # A table: a pipe row followed by a divider row. Anything else starting with "|" is text.
        if line.startswith("|") and i + 1 < len(lines) and _is_divider(lines[i + 1]):
            rows = [_split_row(line)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            width_ok = max(len(r) for r in rows) == len(rows[0])
            if width_ok:
                out.append(Spacer(1, 3))
                out.append(_table(rows, st, width))
                out.append(Spacer(1, 7))
                continue
            # Ragged table: render the rows as text rather than dropping them.
            for r in rows:
                out.append(Paragraph(inline(" · ".join(r)), st["body"]))
            continue

        if line.startswith("### "):
            out.append(Paragraph(inline(line[4:]), st["h3"]))
        elif line.startswith("## "):
            out.append(Paragraph(inline(line[3:]), st["h2"]))
        elif line.startswith("# "):
            out.append(Paragraph(inline(line[2:]), st["h1"]))
        elif line.startswith("> "):
            out.append(Paragraph(inline(line[2:]), st["quote"]))
        elif line.startswith("- "):
            out.append(Paragraph(inline(line[2:]), st["li"], bulletText="•"))
        elif set(line) <= {"-"} and len(line) >= 3:
            out.append(Spacer(1, 6))
            out.append(HRFlowable(width="100%", thickness=0.6, color=LINE,
                                  spaceBefore=0, spaceAfter=6))
        elif line.startswith("**Incident**") or line.startswith("**Detected**"):
            # The two identification lines under the title, set tighter than body text.
            out.append(Paragraph(inline(line), st["meta"]))
        elif line.startswith("_Generated by AIRA"):
            out.append(Paragraph(inline(line.strip("_")), st["foot"]))
        else:
            out.append(Paragraph(inline(line), st["body"]))
        i += 1
    return out


def _page_furniture(title: str):
    """A rule and a page number on every page, so a printed copy stays identifiable."""
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(INK_3)
        canvas.drawString(18 * mm, 12 * mm, title[:95])
        canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, f"page {canvas.getPageNumber()}")
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.5)
        canvas.line(18 * mm, 15 * mm, A4[0] - 18 * mm, 15 * mm)
        canvas.restoreState()
    return draw


def render(md: str, title: str = "AIRA post-incident report") -> bytes:
    """Typeset the report. Returns the PDF bytes."""
    buf = BytesIO()
    margin = 18 * mm
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=22 * mm,
        title=title, author="AIRA", subject="Post-incident report",
    )
    st = _styles()
    width = A4[0] - 2 * margin
    story = flowables(md, st, width)
    doc.build(story, onFirstPage=_page_furniture(title), onLaterPages=_page_furniture(title))
    return buf.getvalue()
