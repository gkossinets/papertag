"""Helpers that build small real PDFs for tests."""

from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import pikepdf

# A line is its text (drawn at 12 pt), or (text, size), or (text, size, "rotated")
# for text turned 90 degrees, like the arXiv identifier in a preprint's margin.
Line = Union[str, Tuple[str, float], Tuple[str, float, str]]


def xmp_packet(description: str) -> str:
    """Wrap XMP properties in a complete packet.

    Args:
        description: The inside of one ``rdf:Description`` element: its
            attributes (namespace declarations, simple properties) followed by
            ``>`` and any child elements. Written raw so tests can use the
            exact namespaces publishers use (several PRISM versions, pdfx,
            crossmark).

    Returns:
        An XMP packet as a string.
    """
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        f'<rdf:Description rdf:about="" {description}\n'
        '</rdf:Description>\n'
        '</rdf:RDF>\n'
        '</x:xmpmeta>\n'
        '<?xpacket end="w"?>'
    )


def make_text_pdf(
    path: Path,
    lines: List[Line],
    docinfo: Optional[Dict[str, str]] = None,
    xmp: Optional[str] = None,
) -> Path:
    """Write a one-page PDF whose page shows `lines` in Helvetica.

    Args:
        path: Where to write the PDF.
        lines: Text lines to draw, top to bottom, each a string (12 pt) or a
            ``(text, size)`` / ``(text, size, "rotated")`` tuple. Lines are
            spaced 1.2 times their size apart.
        docinfo: Document info entries, keyed with or without the leading
            slash (e.g. ``{"/Title": "..."}``).
        xmp: A raw XMP packet (see ``xmp_packet``) stored as the catalog's
            /Metadata stream, bypassing pikepdf's docinfo/XMP sync.

    Returns:
        The path written.
    """
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name.Helvetica,
    ))
    ops = ["BT"]
    y = 740.0
    for line in lines:
        if isinstance(line, str):
            text, size, style = line, 12.0, ""
        else:
            text, size = line[0], line[1]
            style = line[2] if len(line) > 2 else ""
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        y -= 1.2 * size
        matrix = f"0 1 -1 0 30 {y:.1f}" if style == "rotated" else f"1 0 0 1 72 {y:.1f}"
        ops.append(f"/F1 {size} Tf {matrix} Tm ({escaped}) Tj")
    ops.append("ET")
    content = pdf.make_stream("\n".join(ops).encode("latin-1"))
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=[0, 0, 612, 792],
        Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
        Contents=content,
    )
    pdf.pages.append(pikepdf.Page(page))
    if docinfo:
        info = pikepdf.Dictionary()
        for key, value in docinfo.items():
            info[key if key.startswith("/") else f"/{key}"] = pikepdf.String(value)
        pdf.trailer.Info = pdf.make_indirect(info)
    if xmp is not None:
        stream = pdf.make_stream(xmp.encode("utf-8"))
        stream.Type = pikepdf.Name.Metadata
        stream.Subtype = pikepdf.Name.XML
        pdf.Root.Metadata = stream
    # fix_metadata_version would parse the XMP and replace a malformed packet
    # with an empty one; tests need it stored exactly as given.
    pdf.save(path, fix_metadata_version=False)
    return path
