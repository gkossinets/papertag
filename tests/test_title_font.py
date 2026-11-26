"""Title from the largest text on page 1 (roadmap step 3.3).

Publishers set the title in the largest type on the first page. Guessing by
line order and capitalization picked running headers instead ("ORIGINAL
ARTICLE", the journal name), so font size comes first. Layouts below mimic
ones seen in real publisher PDFs, with invented text.
"""

from papertag.core import PDFProcessor
from papertag.core.pdf_processor import TextChunk
from tests.pdf_fixtures import make_text_pdf

BODY = [("Wells in the lower basin were sampled monthly over three dry seasons.", 10)] * 8


def extract(tmp_path, lines):
    path = make_text_pdf(tmp_path / "paper.pdf", lines)
    return PDFProcessor(use_ocr=False).extract_metadata(str(path))


def test_title_is_the_largest_text(tmp_path):
    meta = extract(tmp_path, [
        ("ORIGINAL ARTICLE", 9),
        ("Groundwater Recharge in Dry Mountain Valleys", 18),
        ("Ana Moreno and Lee Park", 12),
        *BODY,
    ])
    assert meta.title == "Groundwater Recharge in Dry Mountain Valleys"
    assert meta.title_source == "font size"


def test_title_lines_are_joined(tmp_path):
    meta = extract(tmp_path, [
        ("Groundwater Recharge in Dry", 18),
        ("Mountain Valleys of the South", 18),
        ("Ana Moreno", 12),
        *BODY,
    ])
    assert meta.title == "Groundwater Recharge in Dry Mountain Valleys of the South"


def test_rotated_margin_text_is_ignored(tmp_path):
    # arXiv stamps its identifier sideways in the margin, larger than the title.
    meta = extract(tmp_path, [
        ("arXiv:2601.00001v1 [cs.LG] 5 Jan 2026", 20, "rotated"),
        ("Sparse Models for Soil Moisture", 16),
        *BODY,
    ])
    assert meta.title == "Sparse Models for Soil Moisture"


def test_short_labels_and_drop_caps_are_not_titles(tmp_path):
    meta = extract(tmp_path, [
        ("FIELD NOTES", 32),
        ("Seasonal Patterns of Bird Migration", 22),
        ("L", 80),
        *BODY,
    ])
    assert meta.title == "Seasonal Patterns of Bird Migration"


def test_journal_name_a_little_larger_than_the_title(tmp_path):
    # Elsevier sets the journal name at ~13.9 pt and the title at ~13.4 pt.
    meta = extract(tmp_path, [
        ("Journal of Valley Studies", 13.9),
        ("Groundwater recharge and land use change in dry mountain valleys", 13.4),
        ("Ana Moreno", 10.6),
        *BODY,
    ])
    assert meta.title == "Groundwater recharge and land use change in dry mountain valleys"


def test_same_size_journal_name_apart_from_the_title(tmp_path):
    # A download cover page: journal name and title in the same type, with
    # the publisher's small print between them.
    meta = extract(tmp_path, [
        ("Valley Studies Quarterly", 16),
        ("Publication details, including instructions for authors", 10),
        ("Groundwater Recharge in Dry Mountain Valleys", 16),
        *BODY,
    ])
    assert meta.title == "Groundwater Recharge in Dry Mountain Valleys"


def test_title_lines_far_apart_are_still_one_title(tmp_path):
    # pypdf reports a wrong height for lines after a line break in some
    # publishers' PDFs, so position must not split a title; only other text
    # drawn in between does.
    meta = extract(tmp_path, [
        ("Groundwater Recharge in", 18),
        ("", 18), ("", 18), ("", 18), ("", 18), ("", 18),
        ("Dry Mountain Valleys", 18),
        *BODY,
    ])
    assert meta.title == "Groundwater Recharge in Dry Mountain Valleys"


def test_jstor_cover_page_fields_are_cut(tmp_path):
    meta = extract(tmp_path, [
        ("Groundwater Recharge in Dry Mountain Valleys Author(s): Ana Moreno "
         "Source: Valley Studies, Vol. 3, No. 2 (1991), pp. 1-20", 14),
        *BODY,
    ])
    assert meta.title == "Groundwater Recharge in Dry Mountain Valleys"


def test_uniform_size_page_falls_back_to_line_heuristic(tmp_path):
    meta = extract(tmp_path, [
        "Groundwater Recharge in Dry Mountain Valleys",
        "Ana Moreno",
        *[line for line, _ in BODY],
    ])
    assert meta.title_source == "text"


def test_footnote_marker_does_not_cut_the_title(tmp_path):
    meta = extract(tmp_path, [
        ("Groundwater Recharge in Dry", 18),
        ("*", 10),
        ("Mountain Valleys", 18),
        ("Ana Moreno", 12),
        *BODY,
    ])
    assert meta.title == "Groundwater Recharge in Dry Mountain Valleys"


def test_embedded_title_still_wins(tmp_path):
    path = make_text_pdf(tmp_path / "paper.pdf", [
        ("Groundwater Recharge in Dry Mountain Valleys", 18), *BODY,
    ], docinfo={"/Title": "Groundwater Recharge in Dry Mountain Valleys"})
    meta = PDFProcessor(use_ocr=False).extract_metadata(str(path))
    assert meta.title_source == "docinfo /Title"


def test_title_with_a_publisher_name_inside_a_word(tmp_path):
    # "Usage" contains "sage"; publisher names must match as whole words.
    meta = extract(tmp_path, [("Chatbot Usage Among Early Adopters", 18), *BODY])
    assert meta.title == "Chatbot Usage Among Early Adopters"


def test_line_heuristic_keeps_titles_with_publisher_name_inside_a_word():
    lines = ["Chatbot Usage Among Early Adopters", "Ana Moreno"]
    title, _, _ = PDFProcessor(use_ocr=False)._extract_metadata_from_text("\n".join(lines))
    assert title == "Chatbot Usage Among Early Adopters"


def test_chunks_with_implausible_size_do_not_split_a_title():
    # pypdf reports size 1.0 for some text after a font switch mid-line
    # (an italic word in the title). That size is noise, not a new block.
    chunks = [
        TextChunk("Groundwater Recharge in", 18.0), TextChunk(" Dry", 1.0),
        TextChunk("\n", None), TextChunk("Mountain Valleys", 18.0), TextChunk("\n", None),
        *[TextChunk("Wells in the lower basin were sampled monthly. ", 10.0)] * 10,
    ]
    title = PDFProcessor(use_ocr=False)._title_from_font_size(chunks)
    assert title == "Groundwater Recharge in Mountain Valleys"


def test_font_title_beats_an_embedded_title_it_extends(tmp_path):
    # Some publishers embed a title cut at the colon.
    path = make_text_pdf(tmp_path / "paper.pdf", [
        ("Groundwater Recharge: Evidence from Dry Mountain Valleys", 18), *BODY,
    ], docinfo={"/Title": "Groundwater Recharge"})
    meta = PDFProcessor(use_ocr=False).extract_metadata(str(path))
    assert meta.title == "Groundwater Recharge: Evidence from Dry Mountain Valleys"
    assert meta.title_source == "font size"
