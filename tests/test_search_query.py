"""The query papertag sends to Crossref, built from page metadata and filename hints.

Pins today's precedence: page metadata wins over the filename. Step 3.3 plans to
let a reliable filename title beat a page-text guess; change these tests then.
"""

from papertag.cli import search_query
from papertag.core import FilenameHints, PDFMetadata


def test_page_metadata_wins_over_filename():
    meta = PDFMetadata(title="ORIGINAL ARTICLE", authors="Ana Moreno", year=None)
    hints = FilenameHints(author="Moreno", year="2019", title="Groundwater Recharge", confidence=0.9)
    assert search_query(meta, hints) == ("ORIGINAL ARTICLE", "Ana Moreno", "2019")


def test_filename_fills_gaps():
    meta = PDFMetadata()
    hints = FilenameHints(author="Moreno", year="2019", title="Groundwater Recharge", confidence=0.9)
    assert search_query(meta, hints) == ("Groundwater Recharge", "Moreno", "2019")
