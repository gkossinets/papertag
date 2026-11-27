"""DOIs read from page 1 (step 3.6).

A page can carry DOIs that aren't the paper's own: a cited work in a footnote,
the DOI of the supplementary material, or the paper's DOI damaged by text
extraction (broken at a line end, the next word glued on). papertag collects
every DOI on the page, repairs what it can, and trusts a looked-up record only
if its title is on the page. Examples are invented.
"""

from unittest.mock import MagicMock

import pytest

from papertag.cli import lookup_doi
from papertag.core import CrossrefAPIError, CrossrefMatch, PDFMetadata, PDFProcessor
from papertag.core.crossref_client import CrossrefClient

PAGE = """Journal of Valley Studies 12 (2019) 1-20
Glacial Valleys of the Northern Highlands
Ada Moreno and Ben Okafor
Abstract. We map the glacial valleys of the northern highlands using
field surveys and elevation models, and compare their shapes.
"""


@pytest.fixture
def processor():
    return PDFProcessor(use_ocr=False)


# --- finding DOIs on the page -------------------------------------------------------

def test_all_dois_in_priority_order(processor):
    text = "See 10.5555/other.7 for data.\nDOI: 10.1234/valleys.1\n"
    assert processor._extract_dois(text) == ["10.1234/valleys.1", "10.5555/other.7"]
    assert processor._extract_doi(text) == "10.1234/valleys.1"


def test_doi_broken_after_a_hyphen_is_rejoined(processor):
    text = "https://doi.org/10.1234/annurev-valleys-012119-\n045404\nCopyright 2020"
    assert processor._extract_dois(text) == ["10.1234/annurev-valleys-012119-045404"]


@pytest.mark.parametrize("text, doi", [
    ("Supporting information: doi:10.1234/pnas.2000123/-/DCSupplemental.", "10.1234/pnas.2000123"),
    ("Supplemental materials: https://doi.org/10.1234/apl0001234.supp", "10.1234/apl0001234"),
])
def test_supplement_suffix_is_stripped(processor, text, doi):
    """The supplement's own record is titled "Supplemental Material for ..."."""
    assert processor._extract_dois(text) == [doi]


def test_glued_word_copy_is_dropped(processor):
    """Footers glue the next word onto the DOI; the clean copy stands for both."""
    text = "DOI: 10.1234/0012345678901\n...\n10.1234/0012345678901journal of valleys"
    assert processor._extract_dois(text) == ["10.1234/0012345678901"]


def test_glued_copy_first_still_yields_the_clean_one(processor):
    """Kept second: the shorter copy could be the damaged one (see below)."""
    text = "doi:10.1234/0012345678901the\nhttps://doi.org/10.1234/0012345678901\n"
    assert processor._extract_dois(text) == ["10.1234/0012345678901the", "10.1234/0012345678901"]


def test_truncated_later_copy_does_not_replace_the_full_doi(processor):
    """A DOI wrapped without a hyphen is cut at the line end."""
    text = "doi: 10.1234/j.abcd.2020.01.001\nCite as: https://doi.org/10.1234/j.ab\ncd.2020.01.001"
    assert processor._extract_dois(text) == ["10.1234/j.abcd.2020.01.001", "10.1234/j.ab"]


@pytest.mark.parametrize("sep", [" ", "\n"])
def test_adjacent_bare_dois_are_all_found(processor, sep):
    text = sep.join(["Cited", "10.1234/aaa.1", "10.1234/bbb.2", "10.1234/ccc.3", "end"])
    assert processor._extract_dois(text) == ["10.1234/aaa.1", "10.1234/bbb.2", "10.1234/ccc.3"]


def test_url_path_after_doi_is_dropped(processor):
    text = "doi:10.1234/valleys/abc012\nhttps://academic.example/doi/10.1234/valleys/abc012/9002026"
    assert processor._extract_dois(text) == ["10.1234/valleys/abc012"]


def test_distinct_dois_sharing_a_prefix_are_kept(processor):
    text = "doi:10.1234/valleys.1 and doi:10.1234/valleys.12"
    assert processor._extract_dois(text) == ["10.1234/valleys.1", "10.1234/valleys.12"]


def test_extract_metadata_records_candidates(processor, tmp_path):
    from tests.pdf_fixtures import make_text_pdf
    path = make_text_pdf(tmp_path / "p.pdf", ["Glacial Valleys of the Northern Highlands",
                                               "doi:10.1234/valleys.1", "see 10.5555/other.7"])
    meta = processor.extract_metadata(str(path))
    assert meta.doi == "10.1234/valleys.1" and meta.doi_source == "text"
    assert meta.doi_candidates == ["10.1234/valleys.1", "10.5555/other.7"]


# --- does the record belong to this page? -------------------------------------------

def _record(title: str, doi: str = "10.1234/valleys.1") -> CrossrefMatch:
    return CrossrefMatch(doi=doi, title=title, authors=["Ada Moreno"], year="2019",
                         journal="Journal of Valley Studies", score=1.0)


@pytest.fixture
def client():
    return CrossrefClient(email="test@example.invalid")


def test_record_whose_title_is_on_the_page(client):
    assert client.shown_on_page(_record("Glacial valleys of the northern highlands"), PAGE)


def test_record_with_markup_in_title(client):
    assert client.shown_on_page(_record("Glacial <i>valleys</i> of the northern highlands"), PAGE)


def test_record_of_another_paper(client):
    assert not client.shown_on_page(_record("Coral reef recovery after storms"), PAGE)


@pytest.mark.parametrize("prefix", ["Supplemental Material for", "Supplementary Materials for",
                                    "Supporting Information for", "Supplementary data to"])
def test_supplement_record_is_rejected(client, prefix):
    """Its title quotes the paper's, so the words are on the page."""
    record = _record(f"{prefix} Glacial Valleys of the Northern Highlands")
    assert not client.shown_on_page(record, PAGE)


@pytest.mark.parametrize("title", ["", "S1"])
def test_record_without_title_words_is_rejected(client, title):
    """Nothing to confirm it by (component and dataset records)."""
    assert not client.shown_on_page(_record(title), PAGE)


def test_run_together_page_words_still_count(client):
    """Some text layers drop the spaces between words."""
    page = ("Journal of Valley Studies 12 (2019) 1-20\nGlacialValleysoftheNorthernHighlands\n"
            "Ada Moreno and Ben Okafor\nAbstract. We compare survey methods across twelve sites "
            "and report the shapes we measured in each season.\n")
    assert client.shown_on_page(_record("Glacial valleys of the northern highlands"), page)


def test_unspaced_title_wrapped_across_lines(client):
    """Chinese and Japanese titles have no spaces, so a whole title is one word."""
    page = PAGE + "\n高山草甸植物\n多样性的变化\n"
    assert client.shown_on_page(_record("高山草甸植物多样性的变化"), page)


def test_too_little_text_to_tell(client):
    assert client.shown_on_page(_record("Coral reef recovery after storms"), "Scanned page")


# --- lookup order ------------------------------------------------------------------

def _meta(**kw) -> PDFMetadata:
    base = dict(doi="10.5555/other.7", doi_source="text",
                doi_candidates=["10.5555/other.7", "10.1234/valleys.1"], extracted_text=PAGE)
    base.update(kw)
    return PDFMetadata(**base)


def _client(records):
    """A client whose fetch_metadata serves `records` by DOI, 404 otherwise."""
    real = CrossrefClient(email="test@example.invalid")
    fake = MagicMock()
    fake.shown_on_page.side_effect = real.shown_on_page

    def fetch(doi):
        if doi not in records:
            raise CrossrefAPIError("API error 404: Resource not found.")
        return records[doi]
    fake.fetch_metadata.side_effect = fetch
    return fake


def test_first_page_doi_that_matches_the_page_wins():
    own = _record("Glacial valleys of the northern highlands")
    client = _client({"10.5555/other.7": _record("Coral reef recovery", "10.5555/other.7"),
                      "10.1234/valleys.1": own})
    assert lookup_doi(client, _meta()) is own


def test_unresolvable_page_doi_moves_on():
    own = _record("Glacial valleys of the northern highlands")
    assert lookup_doi(_client({"10.1234/valleys.1": own}), _meta()) is own


def test_no_page_doi_matches():
    client = _client({"10.5555/other.7": _record("Coral reef recovery", "10.5555/other.7")})
    assert lookup_doi(client, _meta(doi_candidates=["10.5555/other.7"])) is None


def test_rejected_page_records_are_kept_for_the_user():
    """A right record can fail the check (damaged text); interactive users still see it."""
    other = _record("Coral reef recovery", "10.5555/other.7")
    rejected = []
    lookup_doi(_client({"10.5555/other.7": other}), _meta(doi_candidates=["10.5555/other.7"]),
               rejected=rejected)
    assert [m.doi for m in rejected] == ["10.5555/other.7"]
    assert rejected[0].score == 0.0


def test_embedded_doi_is_trusted_without_the_page_check():
    record = _record("A title the page does not show", "10.1234/emb.2")
    client = _client({"10.1234/emb.2": record})
    meta = _meta(doi="10.1234/emb.2", doi_source="xmp prism:doi", doi_candidates=[])
    assert lookup_doi(client, meta) is record


def test_failed_embedded_lookup_falls_back_to_page_dois():
    own = _record("Glacial valleys of the northern highlands")
    meta = _meta(doi="10.1234/emb.2", doi_source="xmp prism:doi",
                 doi_candidates=["10.1234/valleys.1"])
    assert lookup_doi(_client({"10.1234/valleys.1": own}), meta) is own


def test_lookups_are_capped():
    client = _client({})
    meta = _meta(doi_candidates=[f"10.5555/x.{i}" for i in range(10)])
    assert lookup_doi(client, meta) is None
    assert client.fetch_metadata.call_count == 3


def test_no_doi_at_all():
    client = _client({})
    assert lookup_doi(client, PDFMetadata()) is None
    client.fetch_metadata.assert_not_called()
