"""Dissertations, theses and working papers (step 3.8).

Working papers: SSRN and NBER IDs on page 1 map to their Crossref DOIs and
are checked against the page like any page DOI.

Theses rarely carry an ID, but their title page says what they are: degree,
university, author, year. papertag searches Crossref as usual plus DataCite's
dissertations (many university repositories register there). A record of
type dissertation gets "PhD dissertation, <University>" as its journal. When
no registry has the thesis, interactive mode offers a record read from the
title page itself, last and at 0.00; batch mode never applies it.
Examples are invented.
"""

from unittest.mock import MagicMock

import pytest

from papertag.cli import PDFMetadataManager, thesis_journal, title_page_record
from papertag.core import CrossrefMatch, FilenameHints, PDFMetadata, PDFProcessor
from papertag.core.crossref_client import CrossrefClient
from papertag.core.pdf_processor import ThesisInfo


@pytest.fixture
def processor():
    return PDFProcessor(use_ocr=False)


@pytest.fixture
def client():
    return CrossrefClient(email="test@example.invalid")


# --- working-paper IDs -----------------------------------------------------------------

def test_ssrn_id_becomes_a_doi_after_the_page_dois(processor):
    text = ("Glacial Valleys of the Northern Highlands\nDOI: 10.1234/valleys.1\n"
            "Electronic copy available at: https://ssrn.com/abstract=1234567\n")
    assert processor._page_dois(text) == ["10.1234/valleys.1", "10.2139/ssrn.1234567"]


@pytest.mark.parametrize("text", [
    "NBER WORKING PAPER SERIES\nGLACIAL VALLEYS\nWorking Paper 12345\nhttp://www.nber.org/papers/w12345",
    "NBER Working Paper No. 12345\nGlacial Valleys",
])
def test_nber_working_paper_number(processor, text):
    assert processor._page_dois(text) == ["10.3386/w12345"]


def test_working_paper_number_without_nber_is_not_mapped(processor):
    assert processor._page_dois("Valley Institute Working Paper 12345\n") == []


# --- reading a thesis title page -------------------------------------------------------

PHD_PAGE = """GLACIAL VALLEYS OF THE NORTHERN HIGHLANDS
by
ADA MORENO
A dissertation submitted in partial fulfillment
of the requirements for the degree of
Doctor of Philosophy
(Geography)
in the University of Northfield
2019
"""

MASTERS_PAGE = """Coastal Erosion in the Southern Lowlands
A Thesis Presented to the Faculty of Westbrook College
In Partial Fulfillment of the Requirements for the Degree
Master of Science
by Ben Okafor
May 2021
"""

PRIOR_DEGREES_PAGE = """Glacial Valleys of the Northern Highlands
Ada Moreno
B.A., Westbrook College, 2010
M.A., Westbrook College, 2013
Submitted to the Graduate School of Arts and Sciences of
NORTHFIELD STATE UNIVERSITY
in partial fulfillment of the requirements for the degree of
Doctor of Philosophy
Copyright 2018 Ada Moreno
"""


def test_phd_title_page(processor):
    assert processor._thesis_info(PHD_PAGE) == ThesisInfo(
        degree="PhD dissertation", institution="University of Northfield",
        author="Ada Moreno", year="2019")


def test_masters_title_page(processor):
    assert processor._thesis_info(MASTERS_PAGE) == ThesisInfo(
        degree="Master's thesis", institution="Westbrook College",
        author="Ben Okafor", year="2021")


def test_granting_university_not_an_earlier_degree(processor):
    """PhD title pages list earlier degrees; the PhD and its university win."""
    info = processor._thesis_info(PRIOR_DEGREES_PAGE)
    assert (info.degree, info.institution, info.year) == (
        "PhD dissertation", "Northfield State University", "2018")
    assert info.author is None    # no "by" line; a name alone is a guess


@pytest.mark.parametrize("degree_line, label", [
    ("Doctor of Education", "Doctoral dissertation"),
    ("Doctor of Philosophy (Ph.D.)", "PhD dissertation"),
])
def test_other_doctorates(processor, degree_line, label):
    page = PHD_PAGE.replace("Doctor of Philosophy", degree_line)
    assert processor._thesis_info(page).degree == label


def test_article_mentioning_a_dissertation_is_not_a_thesis(processor):
    page = ("Journal of Valley Studies 12 (2019) 1-20\nGlacial Valleys of the Northern Highlands\n"
            "Ada Moreno, University of Northfield\nThis article draws on the author's doctoral "
            "dissertation.\nAbstract. We map the glacial valleys of the northern highlands.\n")
    assert processor._thesis_info(page) is None


def test_extract_metadata_records_the_thesis(processor, tmp_path):
    from tests.pdf_fixtures import make_text_pdf
    pdf = make_text_pdf(tmp_path / "t.pdf", [(PHD_PAGE.splitlines()[0], 18.0)]
                        + PHD_PAGE.splitlines()[1:])
    assert processor.extract_metadata(str(pdf)).thesis.degree == "PhD dissertation"


# --- DataCite --------------------------------------------------------------------------

def test_datacite_family_comma_given_name_is_flipped(client):
    attributes = {"doi": "10.5555/thesis.1", "titles": [{"title": "Glacial Valleys"}],
                  "creators": [{"name": "Moreno, Ada."}], "publicationYear": 2019,
                  "publisher": "University of Northfield Library",
                  "types": {"resourceTypeGeneral": "Dissertation"}}
    match = client._datacite_match(attributes)
    assert (match.authors, match.work_type) == (["Ada Moreno"], "dissertation")


def test_datacite_dissertation_search(client):
    record = {"id": "10.5555/thesis.1", "attributes": {
        "doi": "10.5555/thesis.1",
        "titles": [{"title": "Glacial valleys of the northern highlands"}],
        "creators": [{"name": "Moreno, Ada"}], "publicationYear": 2019,
        "publisher": "University of Northfield Library",
        "types": {"resourceTypeGeneral": "Dissertation"}}}
    other = {"id": "10.5555/thesis.2", "attributes": dict(
        record["attributes"], doi="10.5555/thesis.2",
        titles=[{"title": "Tidal flats of the western shore"}], creators=[{"name": "Lee, Kim"}])}
    client._make_request = MagicMock(return_value={"data": [other, record]})
    matches = client.search_datacite_theses("Glacial Valleys of the Northern Highlands",
                                            "Ada Moreno", "2019")
    url = client._make_request.call_args[0][0]
    assert url.startswith("https://api.datacite.org/dois?")
    assert "resource-type-id=dissertation" in url
    assert "example.invalid" not in str(client._make_request.call_args)
    assert [m.doi for m in matches] == ["10.5555/thesis.1", "10.5555/thesis.2"]
    assert matches[0].score >= 0.80 > matches[1].score


def test_datacite_search_needs_a_title(client):
    client._make_request = MagicMock()
    assert client.search_datacite_theses(None, "Ada Moreno", "2019") == []
    client._make_request.assert_not_called()


# --- journal label and the title-page record -------------------------------------------

THESIS = ThesisInfo(degree="PhD dissertation", institution="University of Northfield",
                    author="Ada Moreno", year="2019")


def _match(work_type, journal=None, doi="10.5555/thesis.1"):
    return CrossrefMatch(doi=doi, title="Glacial valleys of the northern highlands",
                         authors=["Ada Moreno"], year="2019", journal=journal, score=0.9,
                         work_type=work_type)


def test_dissertation_record_gets_degree_and_page_university():
    match = thesis_journal(_match("dissertation", "University of Northfield Library"), THESIS)
    assert match.journal == "PhD dissertation, University of Northfield"


def test_dissertation_record_without_a_title_page():
    match = thesis_journal(_match("dissertation", "University of Northfield Library"), None)
    assert match.journal == "Dissertation, University of Northfield Library"


def test_other_records_keep_their_journal():
    assert thesis_journal(_match("journal-article", "Valley Studies"), THESIS).journal == "Valley Studies"


def test_title_page_record():
    meta = PDFMetadata(title="GLACIAL VALLEYS OF THE NORTHERN HIGHLANDS", thesis=THESIS)
    record = title_page_record(meta, FilenameHints())
    assert (record.doi, record.score, record.work_type) == ("", 0.0, "dissertation")
    assert record.title == "Glacial Valleys of the Northern Highlands"
    assert (record.authors, record.year) == (["Ada Moreno"], "2019")
    assert record.journal == "PhD dissertation, University of Northfield"


def test_title_page_record_author_from_a_trusted_filename():
    meta = PDFMetadata(title="Glacial Valleys", thesis=ThesisInfo(degree="Thesis"))
    hints = FilenameHints(author="Moreno", year="2019", title="Glacial Valleys", confidence=0.9)
    assert title_page_record(meta, hints).authors == ["Moreno"]


def test_no_title_page_record_without_a_title():
    assert title_page_record(PDFMetadata(thesis=THESIS), FilenameHints()) is None


# --- in the per-file flow --------------------------------------------------------------

PDF_PATH = "/tmp/pmm_thesis_test.pdf"


def _manager(batch):
    mgr = PDFMetadataManager(email="x@y.com", batch_mode=batch)
    mgr.ui = MagicMock()
    mgr.ui.quiet = True
    mgr.pdf_processor = MagicMock()
    mgr.pdf_processor.extract_metadata.return_value = PDFMetadata(
        title="Glacial Valleys of the Northern Highlands", title_source="font size",
        thesis=THESIS)
    mgr.metadata_updater = MagicMock()
    mgr.metadata_updater.generate_zotero_filename.return_value = "new.pdf"
    mgr.metadata_updater.rename_file.return_value = "/tmp/new.pdf"
    mgr.crossref_client = MagicMock()
    mgr.crossref_client.search.return_value = []
    mgr.crossref_client.search_datacite_theses.return_value = []
    mgr.logger = MagicMock()
    return mgr


def test_interactive_offers_the_title_page_record_last():
    mgr = _manager(batch=False)
    found = _match("dissertation", "University of Northfield Library", doi="10.5555/thesis.9")
    found.score = 0.5
    mgr.crossref_client.search_datacite_theses.return_value = [found]
    mgr.ui.display_matches.return_value = None   # skip
    mgr.process_single_pdf(PDF_PATH)
    offered = mgr.ui.display_matches.call_args[0][0]
    assert [m.doi for m in offered] == ["10.5555/thesis.9", ""]
    assert offered[0].journal == "PhD dissertation, University of Northfield"
    assert offered[-1].score == 0.0


def test_picking_the_title_page_record_writes_no_doi():
    mgr = _manager(batch=False)
    mgr.ui.display_matches.side_effect = lambda matches, *a: matches[-1]
    mgr.ui.confirm_metadata.return_value = True
    assert mgr.process_single_pdf(PDF_PATH) == "completed"
    written = mgr.metadata_updater.update_metadata.call_args[0][1]
    assert (written.doi, written.journal) == (None, "PhD dissertation, University of Northfield")
    assert mgr.logger.log_success.call_args.kwargs["doi"] is None


def test_batch_never_applies_the_title_page_record():
    mgr = _manager(batch=True)
    assert mgr.process_single_pdf(PDF_PATH) == "skipped"
    mgr.metadata_updater.update_metadata.assert_not_called()


def test_batch_accepts_a_datacite_thesis_record():
    mgr = _manager(batch=True)
    found = _match("dissertation", "University of Northfield Library", doi="10.5555/thesis.9")
    found.score = 0.95
    mgr.crossref_client.search_datacite_theses.return_value = [found]
    assert mgr.process_single_pdf(PDF_PATH) == "completed"
    written = mgr.metadata_updater.update_metadata.call_args[0][1]
    assert (written.doi, written.journal) == ("10.5555/thesis.9",
                                             "PhD dissertation, University of Northfield")


def test_no_datacite_search_for_other_pdfs():
    mgr = _manager(batch=True)
    mgr.pdf_processor.extract_metadata.return_value = PDFMetadata(title="Glacial Valleys")
    mgr.process_single_pdf(PDF_PATH)
    mgr.crossref_client.search_datacite_theses.assert_not_called()


def test_ui_says_where_a_doi_less_record_came_from(capsys, monkeypatch):
    from papertag.ui.interactive import InteractiveUI
    monkeypatch.setattr("builtins.input", lambda *a: "s")
    record = title_page_record(PDFMetadata(title="Glacial Valleys", thesis=THESIS), FilenameHints())
    InteractiveUI().display_matches([record], "t.pdf", FilenameHints())
    assert "DOI: (none; read from the title page)" in capsys.readouterr().out


@pytest.mark.parametrize("line, institution", [
    ("THE UNIVERSITY OF NORTHFIELD", "University of Northfield"),
    ("The University of Northfield", "University of Northfield"),
    ("the University at Westbrook, The State University of Northland", "University at Westbrook"),
    ("the College of Arts and Sciences of Northfield University", "Northfield University"),
])
def test_university_names(processor, line, institution):
    page = PHD_PAGE.replace("in the University of Northfield", f"in {line}")
    assert processor._thesis_info(page).institution == institution


def test_title_page_record_is_confirmed_before_writing():
    """Nothing checked it, and its author may be missing: show it, as for a typed DOI."""
    mgr = _manager(batch=False)
    mgr.ui.display_matches.side_effect = lambda matches, *a: matches[-1]
    mgr.ui.confirm_metadata.return_value = False
    assert mgr.process_single_pdf(PDF_PATH) == "skipped"
    mgr.ui.confirm_metadata.assert_called_once()
    mgr.metadata_updater.update_metadata.assert_not_called()


def test_registry_record_pick_still_skips_confirmation():
    mgr = _manager(batch=False)
    found = _match("dissertation", "University of Northfield Library", doi="10.5555/thesis.9")
    mgr.crossref_client.search_datacite_theses.return_value = [found]
    mgr.ui.display_matches.side_effect = lambda matches, *a: matches[0]
    assert mgr.process_single_pdf(PDF_PATH) == "completed"
    mgr.ui.confirm_metadata.assert_not_called()


def test_title_page_record_uses_the_search_title():
    """A line-order guess loses to a well-formed filename's title, as in search."""
    meta = PDFMetadata(title="A Dissertation Submitted to the Faculty", title_source="text",
                       thesis=THESIS)
    hints = FilenameHints(author="Moreno", year="2019", title="Glacial Valleys", confidence=0.9)
    assert title_page_record(meta, hints).title == "Glacial Valleys"
