"""The query papertag sends to Crossref, built from page metadata and filename hints.

A title guessed from line order and capitalization (title_source "text") is
often a running header, so a well-formed filename's title beats it. Titles
from embedded metadata or the largest type on the page beat the filename.
"""

from papertag.cli import search_query
from papertag.core import FilenameHints, PDFMetadata

HINTS = FilenameHints(author="Moreno", year="2019", title="Groundwater Recharge", confidence=0.9)


def test_filename_title_beats_a_line_order_guess():
    meta = PDFMetadata(title="ORIGINAL ARTICLE", title_source="text", authors="Ana Moreno")
    assert search_query(meta, HINTS) == ("Groundwater Recharge", "Moreno", "2019", True)


def test_line_order_guess_beats_a_loose_filename():
    hints = FilenameHints(author="Moreno", year="2019", title="final draft", confidence=0.75)
    meta = PDFMetadata(title="Groundwater Recharge", title_source="text")
    assert search_query(meta, hints)[0] == "Groundwater Recharge"


def test_font_size_title_beats_filename():
    meta = PDFMetadata(title="Groundwater Recharge in Dry Valleys", title_source="font size")
    assert search_query(meta, HINTS)[0] == "Groundwater Recharge in Dry Valleys"


def test_embedded_title_beats_filename():
    meta = PDFMetadata(title="Groundwater Recharge in Dry Valleys", title_source="docinfo /Title")
    assert search_query(meta, HINTS)[0] == "Groundwater Recharge in Dry Valleys"


def test_filename_fills_gaps():
    meta = PDFMetadata()
    hints = FilenameHints(author="Moreno", year="2019", title="Groundwater Recharge", confidence=0.9)
    assert search_query(meta, hints) == ("Groundwater Recharge", "Moreno", "2019", True)


def test_filename_author_beats_page_text_author():
    # The page-text author is a line-order guess too, right for about one
    # file in six on a real library; a wrong author caps the match score.
    meta = PDFMetadata(title="Groundwater Recharge in Dry Valleys", title_source="font size",
                       authors="Department of Earth Sciences, Valley University")
    assert search_query(meta, HINTS)[1] == "Moreno"


def test_page_text_author_fills_a_loose_filename():
    hints = FilenameHints(author="draft", year=None, title=None, confidence=0.6)
    meta = PDFMetadata(authors="Ana Moreno")
    assert search_query(meta, hints)[1] == "Ana Moreno"


def test_page_text_author_is_marked_unreliable():
    hints = FilenameHints(author="draft", year=None, title=None, confidence=0.6)
    query = search_query(PDFMetadata(authors="Ana Moreno"), hints)
    assert query.author_reliable is False


def test_filename_author_is_reliable():
    query = search_query(PDFMetadata(authors="Uppsala University, Sweden"), HINTS)
    assert query.author == "Moreno" and query.author_reliable is True


def test_filename_author_is_reliable_even_when_the_page_shows_the_same_text():
    query = search_query(PDFMetadata(authors="Moreno"), HINTS)
    assert query.author == "Moreno" and query.author_reliable is True
