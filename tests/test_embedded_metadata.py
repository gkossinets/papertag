"""DOI and title already embedded in the PDF (XMP, docinfo) beat text heuristics."""

import logging

import pytest

from papertag.core.pdf_processor import PDFProcessor
from tests.pdf_fixtures import make_text_pdf, xmp_packet

DC = 'xmlns:dc="http://purl.org/dc/elements/1.1/"'
PDFX = 'xmlns:pdfx="http://ns.adobe.com/pdfx/1.3/"'
CROSSMARK = 'xmlns:crossmark="http://crossref.org/crossmark/1.0/"'

# Page text with no DOI and a title the line heuristic would pick.
BODY = ["SOME RUNNING HEADER TEXT", "Jane Doe and John Roe",
        "Abstract. We study things at length in this paper, with",
        "methods, data and results described on the pages below."]


def extract(tmp_path, lines=BODY, docinfo=None, xmp=None):
    path = make_text_pdf(tmp_path / "paper.pdf", lines, docinfo=docinfo, xmp=xmp)
    return PDFProcessor(use_ocr=False).extract_metadata(str(path))


# --- DOI ---------------------------------------------------------------------

@pytest.mark.parametrize("version", ["2.0", "2.1", "3.0"])
def test_doi_from_prism(tmp_path, version):
    ns = f'xmlns:prism="http://prismstandard.org/namespaces/basic/{version}/"'
    meta = extract(tmp_path, xmp=xmp_packet(f'{ns} prism:doi="10.1016/j.jhydrol.2019.01.001">'))
    assert meta.doi == "10.1016/j.jhydrol.2019.01.001"
    assert meta.doi_source == "xmp prism:doi"


def test_doi_from_pdfx(tmp_path):
    meta = extract(tmp_path, xmp=xmp_packet(f'{PDFX} pdfx:doi="10.1111/gcb.14567">'))
    assert meta.doi == "10.1111/gcb.14567"
    assert meta.doi_source == "xmp pdfx:doi"


def test_doi_from_crossmark(tmp_path):
    meta = extract(tmp_path, xmp=xmp_packet(f'{CROSSMARK} crossmark:DOI="10.1029/2019GL082345">'))
    assert meta.doi == "10.1029/2019GL082345"
    assert meta.doi_source == "xmp crossmark:DOI"


@pytest.mark.parametrize("value", [
    "10.1093/gji/ggz123",
    "doi:10.1093/gji/ggz123",
    "DOI: 10.1093/gji/ggz123",
    "info:doi/10.1093/gji/ggz123",
    "https://doi.org/10.1093/gji/ggz123",
    "http://dx.doi.org/10.1093/gji/ggz123",
])
def test_doi_from_dc_identifier_strips_prefixes(tmp_path, value):
    meta = extract(tmp_path, xmp=xmp_packet(f'{DC} dc:identifier="{value}">'))
    assert meta.doi == "10.1093/gji/ggz123"
    assert meta.doi_source == "xmp dc:identifier"


def test_sici_doi_keeps_trailing_check_character(tmp_path):
    """SICI DOIs can end in '#', their check character; it is part of the DOI."""
    doi = "10.1002/(SICI)1097-0088(199901)19:1&lt;1::AID-JOC350&gt;3.0.CO;2-#"
    meta = extract(tmp_path, xmp=xmp_packet(f'{PDFX} pdfx:doi="{doi}">'))
    assert meta.doi == "10.1002/(SICI)1097-0088(199901)19:1<1::AID-JOC350>3.0.CO;2-#"


@pytest.mark.parametrize("value", [
    "https://www.doi.org/10.1093/gji/ggz123",
    "doi.org/10.1093/gji/ggz123",
    "urn:doi:10.1093/gji/ggz123",
    "doi 10.1093/gji/ggz123",
    "10.1093/gji/ggz123.",
])
def test_more_doi_forms_in_structured_fields(tmp_path, value):
    meta = extract(tmp_path, docinfo={"/DOI": value})
    assert meta.doi == "10.1093/gji/ggz123"


def test_doi_from_docinfo_doi_url(tmp_path):
    meta = extract(tmp_path, docinfo={"/DOI": "https://doi.org/10.48550/arXiv.2101.00001"})
    assert meta.doi == "10.48550/arXiv.2101.00001"
    assert meta.doi_source == "docinfo /DOI"




def test_doi_from_subject_publisher_citation(tmp_path):
    meta = extract(tmp_path, docinfo={
        "/Subject": "Journal of Things, 12 (2020) 1-10. doi:10.1016/j.things.2020.01.001"})
    assert meta.doi == "10.1016/j.things.2020.01.001"


def test_unlabeled_doi_in_subject_is_ignored(tmp_path):
    """A bare DOI in free text could be any work; only a labeled one counts."""
    meta = extract(tmp_path, docinfo={"/Subject": "A reply to 10.1000/other.paper"})
    assert meta.doi is None


def test_journal_doi_is_not_the_article_doi(tmp_path):
    """Wiley's pdfx:WPS-JOURNALDOI names the journal, e.g. 10.1111/(ISSN)1365-2486."""
    meta = extract(tmp_path, xmp=xmp_packet(
        f'{PDFX} pdfx:WPS-JOURNALDOI="10.1111/(ISSN)1365-2486">'))
    assert meta.doi is None


def test_issn_doi_rejected_even_in_allowed_key(tmp_path):
    meta = extract(tmp_path, xmp=xmp_packet(f'{PDFX} pdfx:doi="10.1111/(ISSN)1365-2486">'))
    assert meta.doi is None


def test_non_doi_value_falls_back_to_text(tmp_path):
    lines = BODY + ["doi:10.1234/from.text"]
    meta = extract(tmp_path, lines=lines, xmp=xmp_packet(f'{PDFX} pdfx:doi="n/a">'))
    assert meta.doi == "10.1234/from.text"
    assert meta.doi_source == "text"


def test_embedded_doi_beats_text_doi(tmp_path):
    lines = BODY + ["See also doi:10.9999/some.reference"]
    meta = extract(tmp_path, lines=lines,
                   xmp=xmp_packet(f'{PDFX} pdfx:doi="10.1111/gcb.14567">'))
    assert meta.doi == "10.1111/gcb.14567"


def test_publisher_key_beats_dc_identifier(tmp_path):
    xmp = xmp_packet(f'{PDFX} {DC} pdfx:doi="10.1111/gcb.14567" '
                     f'dc:identifier="10.1111/gcb.00000">')
    assert extract(tmp_path, xmp=xmp).doi == "10.1111/gcb.14567"


def test_conflicting_embedded_dois_are_logged(tmp_path, caplog):
    xmp = xmp_packet(f'{PDFX} {DC} pdfx:doi="10.1111/gcb.14567" '
                     f'dc:identifier="10.1111/gcb.00000">')
    with caplog.at_level(logging.DEBUG, logger="papertag"):
        extract(tmp_path, xmp=xmp)
    assert "10.1111/gcb.00000" in caplog.text


def test_no_embedded_doi_and_none_in_text(tmp_path):
    meta = extract(tmp_path)
    assert meta.doi is None
    assert meta.doi_source is None


# --- Title -------------------------------------------------------------------
# A real title also appears on page 1, so tests put it there.

def with_title(title):
    return BODY + [title]


def test_title_from_docinfo_beats_text_heuristic(tmp_path):
    title = "Groundwater Recharge in Semi-Arid Basins"
    meta = extract(tmp_path, lines=with_title(title), docinfo={"/Title": title})
    assert meta.title == title
    assert meta.title_source == "docinfo /Title"


def test_title_from_xmp_dc_title(tmp_path):
    xmp = xmp_packet(f'{DC}><dc:title><rdf:Alt><rdf:li xml:lang="x-default">'
                     'Glaciers: retreating or advancing?</rdf:li></rdf:Alt></dc:title>')
    meta = extract(tmp_path, lines=with_title("Glaciers: retreating or advancing?"), xmp=xmp)
    assert meta.title == "Glaciers: retreating or advancing?"
    assert meta.title_source == "xmp dc:title"


def test_title_markup_and_whitespace_cleaned(tmp_path):
    meta = extract(tmp_path, lines=with_title("Quercus robur: Growth rings and drought"),
                   docinfo={"/Title": "  <i>Quercus robur</i>:  Growth rings\nand drought "})
    assert meta.title == "Quercus robur: Growth rings and drought"


@pytest.mark.parametrize("junk", [
    "",
    "untitled",
    "Untitled-1",
    "Untitled document",
    "untitled document 2",
    "Microsoft Word - final draft v3.docx",
    "Microsoft PowerPoint - slides",
    "PowerPoint Presentation",
    "Slide 1",
    "Copy of Final Draft",
    "Accepted Manuscript",
    "Author's Accepted Manuscript",
    "Manuscript ID HYD-2019-0123",
    "Running head: GROUNDWATER RECHARGE",
    "Electronic copy available at: https://ssrn.com/abstract=1234567",
    "Paper Title (use style: paper title)",
    "Preparation of Papers for IEEE TRANSACTIONS and JOURNALS",
    "Instructions for authors",
    "Proceedings Template - WORD",
    "LaTeX Template",
    "Title of the Article",
    "Article title",
    "paper.pdf",
    "main.dvi",
    "C:\\Users\\me\\paper",
    "Layout 1",
    "PII: S0022-1694(19)00001-2",
    "doi:10.1016/j.jhydrol.2019.01.001",
    "10.1016/j.jhydrol.2019.01.001",
    "arXiv:2101.00001v1",
    "Title",
    "Article",
    "1",
    "jhydrol_2019_01",
])
def test_junk_title_falls_back_to_text(tmp_path, junk):
    """Junk is rejected even when it is printed on the page (an accepted
    manuscript says "Accepted Manuscript" on page 1)."""
    meta = extract(tmp_path, lines=with_title(junk), docinfo={"/Title": junk})
    assert meta.title == "SOME RUNNING HEADER TEXT"
    assert meta.title_source == "text"


def test_short_real_title_is_kept(tmp_path):
    meta = extract(tmp_path, lines=with_title("What is a river?"),
                   docinfo={"/Title": "What is a river?"})
    assert meta.title == "What is a river?"


def test_title_not_on_page_is_rejected(tmp_path):
    """A title the page doesn't show was probably left by a template or tool."""
    meta = extract(tmp_path, docinfo={"/Title": "Notes for the Thursday Meeting"})
    assert meta.title == "SOME RUNNING HEADER TEXT"
    assert meta.title_source == "text"


def test_title_wrapped_and_hyphenated_on_page_is_accepted(tmp_path):
    title = "Groundwater Recharge in Semi-Arid Basins of Southern Europe"
    lines = BODY + ["Groundwater Re-", "charge in Semi-Arid Basins", "of Southern Europe"]
    meta = extract(tmp_path, lines=lines, docinfo={"/Title": title})
    assert meta.title == title


def test_junk_xmp_title_does_not_hide_good_docinfo_title(tmp_path):
    title = "Groundwater Recharge in Semi-Arid Basins"
    xmp = xmp_packet(f'{DC}><dc:title><rdf:Alt><rdf:li xml:lang="x-default">'
                     'Notes for the Thursday Meeting</rdf:li></rdf:Alt></dc:title>')
    meta = extract(tmp_path, lines=with_title(title), docinfo={"/Title": title}, xmp=xmp)
    assert meta.title == title
    assert meta.title_source == "docinfo /Title"


def test_title_accepted_without_text_layer(tmp_path):
    """With no page text to check against, the embedded title is the best guess."""
    meta = extract(tmp_path, lines=[], docinfo={"/Title": "Groundwater Recharge in Semi-Arid Basins"})
    assert meta.title == "Groundwater Recharge in Semi-Arid Basins"


# --- papertag's own earlier output -------------------------------------------
# A re-run must not trust what an earlier run wrote: a wrong batch pick would be
# confirmed instead of fixed. Only publisher keys and page text count then.

PAPERTAG_XMP = xmp_packet(
    f'{DC} dc:identifier="10.1007/s00382-019-04640-2">'
    '<dc:title><rdf:Alt><rdf:li xml:lang="x-default">'
    'An Earlier Possibly Wrong Pick</rdf:li></rdf:Alt></dc:title>')


@pytest.mark.parametrize("docinfo", [
    {"/Subject": "Climate Dynamics | DOI: 10.1007/s00382-019-04640-2",
     "/Keywords": "DOI: 10.1007/s00382-019-04640-2"},
    {"/Subject": "Climate Dynamics | DOI: 10.1007/s00382-019-04640-2"},   # older runs: no /Keywords
    {"/Subject": "DOI: 10.1007/s00382-019-04640-2"},               # no journal
    {"/Keywords": "ISBN: 9780199540884"},
    {"/Subject": "Oxford University Press | ISBN: 9780199540884"},
])
def test_papertag_output_is_ignored(tmp_path, docinfo):
    docinfo = {**docinfo, "/Title": "An Earlier Possibly Wrong Pick"}
    meta = extract(tmp_path, docinfo=docinfo, xmp=PAPERTAG_XMP)
    assert meta.doi is None
    assert meta.title == "SOME RUNNING HEADER TEXT"
    assert meta.title_source == "text"


def test_papertag_output_ignored_text_doi_still_used(tmp_path):
    meta = extract(tmp_path, lines=BODY + ["doi:10.1234/from.text"],
                   docinfo={"/Keywords": "DOI: 10.1007/s00382-019-04640-2"}, xmp=PAPERTAG_XMP)
    assert meta.doi == "10.1234/from.text"
    assert meta.doi_source == "text"


def test_publisher_doi_trusted_in_papertag_processed_file(tmp_path):
    ns = 'xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/"'
    xmp = xmp_packet(f'{ns} {DC} prism:doi="10.1111/gcb.14567" '
                     'dc:identifier="10.1007/s00382-019-04640-2">')
    meta = extract(tmp_path, docinfo={"/Keywords": "DOI: 10.1007/s00382-019-04640-2"}, xmp=xmp)
    assert meta.doi == "10.1111/gcb.14567"
    assert meta.doi_source == "xmp prism:doi"


def test_docinfo_doi_trusted_in_papertag_processed_file(tmp_path):
    meta = extract(tmp_path, docinfo={"/Keywords": "DOI: 10.1007/s00382-019-04640-2",
                                      "/DOI": "https://doi.org/10.48550/arXiv.2101.00001"})
    assert meta.doi == "10.48550/arXiv.2101.00001"


def test_papertag_signature_is_logged(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger="papertag"):
        extract(tmp_path, docinfo={"/Keywords": "DOI: 10.1007/s00382-019-04640-2"})
    assert "papertag" in caplog.text.lower()
    assert "earlier run" in caplog.text


@pytest.mark.parametrize("subject", [
    "Journal of Things, 12 (2020) 1-10. doi:10.1016/j.things.2020.01.001",
    "Keywords: DOI: x",          # not papertag's layout: no DOI after the label
])
def test_publisher_subject_is_not_a_papertag_signature(tmp_path, subject):
    meta = extract(tmp_path, lines=with_title("A Perfectly Good Title"),
                   docinfo={"/Subject": subject, "/Title": "A Perfectly Good Title"})
    assert meta.title == "A Perfectly Good Title"


# --- Robustness --------------------------------------------------------------

def test_malformed_xmp_still_reads_docinfo(tmp_path):
    meta = extract(tmp_path, lines=with_title("A Perfectly Good Title"), xmp="<not xml",
                   docinfo={"/Title": "A Perfectly Good Title", "/DOI": "10.1234/abc.def"})
    assert meta.title == "A Perfectly Good Title"
    assert meta.doi == "10.1234/abc.def"
