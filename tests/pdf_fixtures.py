"""Helpers that build small real PDFs for tests."""

from pathlib import Path
from typing import List

import pikepdf


def make_text_pdf(path: Path, lines: List[str]) -> Path:
    """Write a one-page PDF whose page shows `lines` in Helvetica.

    Args:
        path: Where to write the PDF.
        lines: Text lines to draw, top to bottom.

    Returns:
        The path written.
    """
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name.Helvetica,
    ))
    ops = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({escaped}) Tj T*")
    ops.append("ET")
    content = pdf.make_stream("\n".join(ops).encode("latin-1"))
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=[0, 0, 612, 792],
        Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
        Contents=content,
    )
    pdf.pages.append(pikepdf.Page(page))
    pdf.save(path)
    return path
