"""arXiv IDs and ISBNs (step 3.7).

arXiv registers a DataCite DOI for every paper (10.48550/arXiv.<id>), which
Crossref does not know. papertag reads the arXiv ID from the page-1 margin
stamp or an arXiv-style filename, looks it up in DataCite, and checks the
record against the page like any page DOI. DOIs Crossref doesn't have are
tried in DataCite too (Zenodo, repositories).

ISBNs belong to a book; a chapter PDF carries its book's ISBN. papertag asks
Crossref for that book's records and takes the one whose title is the page's
title. Examples are invented; the ISBNs are made up but checksum-valid.
"""

from unittest.mock import MagicMock, patch

import pytest

from papertag.cli import lookup_doi, lookup_isbn
from papertag.core import (CrossrefAPIError, CrossrefConnectionError, CrossrefMatch,
                           PDFMetadata, PDFProcessor, parse_filename)
from papertag.core.crossref_client import CrossrefClient
from tests.pdf_fixtures import make_text_pdf, xmp_packet

# 978-1-55555-012-7 is ISBN-10 1-55555-012-6; 0-8044-2957-X is 978-0-8044-2957-3.
ISBN13 = "9781555550127"
ISBN10_SAME = "1555550126"


@pytest.fixture
def processor():
    return PDFProcessor(use_ocr=False)


@pytest.fixture
def client():
    return CrossrefClient(email="test@example.invalid")


# --- arXiv IDs on the page -------------------------------------------------------------

@pytest.mark.parametrize("stamp, arxiv_id", [
    ("arXiv:2101.00001v2 [cs.LG] 3 Feb 2021", "2101.00001"),
    ("arXiv:0912.1234v1  [astro-ph.CO]  7 Dec 2009", "0912.1234"),
    ("arXiv:hep-th/9901001v3 [hep-th] 12 Jan 1999", "hep-th/9901001"),
    ("arXiv:math.AG/0101001v1 [math.AG]", "math.AG/0101001"),
])
def test_arxiv_stamp_id_without_version(processor, stamp, arxiv_id):
    assert processor._extract_arxiv_stamp(f"Some text\n{stamp}\nmore") == arxiv_id


@pytest.mark.parametrize("text", [
    "Building on prior work (arXiv preprint arXiv:2005.00002, 2020) we",
    "Code and data: see arXiv:2005.00002.",
    "",
])
def test_cited_arxiv_ids_are_not_the_stamp(processor, text):
    """Only the margin stamp (version and category) says the PDF is that preprint."""
    assert processor._extract_arxiv_stamp(text) is None


def test_stamp_doi_is_the_first_page_candidate(processor, tmp_path):
    pdf = make_text_pdf(tmp_path / "p.pdf", [
        ("arXiv:2101.00001v2 [cs.LG] 3 Feb 2021", 10.0, "rotated"),
        ("Glacial Valleys of the Northern Highlands", 18.0),
        "Ada Moreno and Ben Okafor",
        "Published version: https://doi.org/10.1234/valleys.1",
        "We map the glacial valleys of the northern highlands using field surveys.",
    ])
    meta = processor.extract_metadata(str(pdf))
    assert meta.arxiv_id == "2101.00001"
    assert meta.doi_candidates == ["10.48550/arXiv.2101.00001", "10.1234/valleys.1"]
    assert (meta.doi, meta.doi_source) == ("10.48550/arXiv.2101.00001", "text")


def test_stamp_does_not_repeat_a_page_copy_of_its_doi(processor, tmp_path):
    pdf = make_text_pdf(tmp_path / "p.pdf", [
        ("arXiv:2101.00001v2 [cs.LG] 3 Feb 2021", 10.0, "rotated"),
        ("Glacial Valleys of the Northern Highlands", 18.0),
        "DOI: 10.48550/arxiv.2101.00001",
        "We map the glacial valleys of the northern highlands using field surveys.",
    ])
    assert processor.extract_metadata(str(pdf)).doi_candidates == ["10.48550/arXiv.2101.00001"]


# --- arXiv IDs in filenames ------------------------------------------------------------

@pytest.mark.parametrize("name, arxiv_id", [
    ("2101.00001v2.pdf", "2101.00001"),
    ("2101.00001.pdf", "2101.00001"),
    ("0912.1234.pdf", "0912.1234"),
    ("arXiv-2101.00001v1.pdf", "2101.00001"),
    ("arxiv_2101.00001.pdf", "2101.00001"),
])
def test_arxiv_filename(name, arxiv_id):
    hints = parse_filename(name)
    assert hints.arxiv_id == arxiv_id
    # "2004.12345" is not a year hint, and nothing else in the name is a hint.
    assert (hints.author, hints.year, hints.title) == (None, None, None)


@pytest.mark.parametrize("name", [
    "2019.12345.pdf",                               # month 19: not an arXiv ID
    "Moreno - 2019 - Glacial valleys.pdf",
    "Moreno - 2021 - Valleys 2101.00001.pdf",       # an ID inside a name is not the name
])
def test_not_an_arxiv_filename(name):
    assert parse_filename(name).arxiv_id is None


# --- DataCite --------------------------------------------------------------------------

DATACITE_ARXIV = {"data": {"attributes": {
    "doi": "10.48550/arxiv.2101.00001",
    "titles": [{"title": "Glacial Valleys of the Northern Highlands"},
               {"title": "A field survey", "titleType": "Subtitle"}],
    "creators": [
        {"name": "Moreno, Ada", "nameType": "Personal", "givenName": "Ada",
         "familyName": "Moreno"},
        {"name": "Valley Mapping Group", "nameType": "Organizational"},
    ],
    "publicationYear": 2021,
    "publisher": "arXiv",
    "container": {},
    "types": {"resourceTypeGeneral": "Preprint"},
}}}


def _response(status, payload=None):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.text = "Resource not found." if status == 404 else ""
    response.headers = {}
    return response


@patch("papertag.core.crossref_client.requests.request")
def test_arxiv_doi_goes_straight_to_datacite(request, client):
    request.return_value = _response(200, DATACITE_ARXIV)
    match = client.fetch_metadata("10.48550/arXiv.2101.00001")
    assert request.call_count == 1
    assert request.call_args[0][1] == "https://api.datacite.org/dois/10.48550/arXiv.2101.00001"
    assert match.doi == "10.48550/arxiv.2101.00001"
    assert match.title == "Glacial Valleys of the Northern Highlands"
    assert match.authors == ["Ada Moreno", "Valley Mapping Group"]
    assert (match.year, match.journal, match.score) == ("2021", "arXiv", 1.0)
    assert match.is_preprint


@patch("papertag.core.crossref_client.requests.request")
def test_datacite_requests_carry_no_email(request, client):
    """The email is for Crossref's polite pool; DataCite doesn't need it."""
    request.return_value = _response(200, DATACITE_ARXIV)
    client.fetch_metadata("10.48550/arXiv.2101.00001")
    assert "example.invalid" not in str(request.call_args)


@patch("papertag.core.crossref_client.requests.request")
def test_doi_crossref_lacks_is_tried_in_datacite(request, client):
    record = {"data": {"attributes": dict(
        DATACITE_ARXIV["data"]["attributes"], doi="10.5281/zenodo.123",
        publisher="Zenodo", types={"resourceTypeGeneral": "Report"})}}
    request.side_effect = [_response(404), _response(200, record)]
    match = client.fetch_metadata("10.5281/zenodo.123")
    urls = [call[0][1] for call in request.call_args_list]
    assert urls == ["https://api.crossref.org/works/10.5281/zenodo.123",
                    "https://api.datacite.org/dois/10.5281/zenodo.123"]
    assert (match.doi, match.journal, match.work_type) == ("10.5281/zenodo.123", "Zenodo", "report")


@patch("papertag.core.crossref_client.requests.request")
def test_doi_in_neither_registry(request, client):
    request.side_effect = [_response(404), _response(404)]
    with pytest.raises(CrossrefAPIError):
        client.fetch_metadata("10.1234/missing")


@patch("papertag.core.crossref_client.time.sleep")
@patch("papertag.core.crossref_client.requests.request")
def test_connection_failure_is_not_a_missing_doi(request, sleep, client):
    import requests
    request.side_effect = requests.exceptions.ConnectionError("offline")
    with pytest.raises(CrossrefConnectionError):
        client.fetch_metadata("10.1234/valleys.1")
    assert all("datacite" not in call[0][1] for call in request.call_args_list)


def test_container_title_is_the_journal(client):
    attributes = dict(DATACITE_ARXIV["data"]["attributes"],
                      container={"type": "Series", "title": "Valley Papers"})
    assert client._datacite_match(attributes).journal == "Valley Papers"


# --- the filename's arXiv ID in the lookup order ---------------------------------------

PAGE = """Glacial Valleys of the Northern Highlands
Ada Moreno and Ben Okafor
Abstract. We map the glacial valleys of the northern highlands using
field surveys and elevation models, and compare their shapes.
"""


def _record(title, doi="10.48550/arxiv.2101.00001", work_type="posted-content", isbn=None):
    return CrossrefMatch(doi=doi, title=title, authors=["Ada Moreno"], year="2021",
                         journal="arXiv", score=1.0, work_type=work_type, isbn=isbn)


def _doi_client(records):
    real = CrossrefClient(email="test@example.invalid")
    fake = MagicMock()
    fake.shown_on_page.side_effect = real.shown_on_page

    def fetch(doi):
        if doi not in records:
            raise CrossrefAPIError("API error 404: Resource not found.")
        return records[doi]
    fake.fetch_metadata.side_effect = fetch
    return fake


def test_filename_arxiv_id_is_looked_up_and_checked():
    own = _record("Glacial valleys of the northern highlands")
    client = _doi_client({"10.48550/arXiv.2101.00001": own})
    meta = PDFMetadata(extracted_text=PAGE)
    assert lookup_doi(client, meta, filename_hints=parse_filename("2101.00001v2.pdf")) is own


def test_filename_arxiv_id_comes_after_page_dois():
    """The page says what the PDF is; a filename only says what it was saved as."""
    article = _record("Glacial valleys of the northern highlands", doi="10.1234/valleys.1",
                      work_type="journal-article")
    preprint = _record("Glacial valleys of the northern highlands")
    client = _doi_client({"10.1234/valleys.1": article, "10.48550/arXiv.2101.00001": preprint})
    meta = PDFMetadata(doi="10.1234/valleys.1", doi_source="text",
                       doi_candidates=["10.1234/valleys.1"], extracted_text=PAGE)
    assert lookup_doi(client, meta, filename_hints=parse_filename("2101.00001.pdf")) is article


def test_filename_arxiv_id_already_on_the_page_is_fetched_once():
    own = _record("Glacial valleys of the northern highlands")
    client = _doi_client({"10.48550/arXiv.2101.00001": own})
    meta = PDFMetadata(doi_candidates=["10.48550/arXiv.2101.00001"], extracted_text=PAGE)
    lookup_doi(client, meta, filename_hints=parse_filename("2101.00001v2.pdf"))
    assert client.fetch_metadata.call_count == 1


# --- ISBNs on the page and in XMP ------------------------------------------------------

@pytest.mark.parametrize("text, isbns", [
    ("ISBN 978-1-55555-012-7 (hardback)", [ISBN13]),
    ("ISBN: 9781555550127", [ISBN13]),
    ("ISBN-13: 978 1 55555 012 7", [ISBN13]),
    ("ISBN-10: 1-55555-012-6", [ISBN13]),
    ("ISBN 0-8044-2957-X", ["9780804429573"]),
    ("ISBN 978-1-55555-012-7 (print)\nISBN 978-0-8044-2957-3 (ebook)",
     [ISBN13, "9780804429573"]),
    ("ISBN-10: 1555550126\nISBN-13: 978-1555550127", [ISBN13]),
    ("ISBN 1-55555-012-6 2019", [ISBN13]),          # a year after an ISBN-10
])
def test_page_isbns_normalized_to_13_digits(processor, text, isbns):
    assert processor._extract_isbns(text) == isbns


@pytest.mark.parametrize("text", [
    "ISBN 978-1-55555-012-8",       # wrong check digit
    "Order no. 9781555550127",      # unlabeled: could be any 13-digit number
    "ISBN 12345",
])
def test_not_an_isbn(processor, text):
    assert processor._extract_isbns(text) == []


def test_embedded_isbn_comes_before_the_page_ones(processor, tmp_path):
    xmp = xmp_packet('xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/" '
                     'prism:isbn="978-0-8044-2957-3">')
    pdf = make_text_pdf(tmp_path / "b.pdf", [
        ("Glacial Valleys of the Northern Highlands", 18.0),
        "ISBN 978-1-55555-012-7",
        "A survey of the glacial valleys of the northern highlands and their shapes.",
    ], xmp=xmp)
    assert processor.extract_metadata(str(pdf)).isbn_candidates == ["9780804429573", ISBN13]


# --- ISBN lookup -----------------------------------------------------------------------

def test_isbn_search_url(client):
    url = client.isbn_url(ISBN13, "Glacial Valleys")
    assert url.startswith("https://api.crossref.org/works?")
    assert f"filter=isbn%3A{ISBN13}" in url
    assert "query.bibliographic=Glacial+Valleys" in url


def _item(title, doi, work_type):
    return {"DOI": doi, "title": [title], "type": work_type, "ISBN": [ISBN13],
            "author": [{"given": "Ada", "family": "Moreno"}],
            "issued": {"date-parts": [[2019]]}, "container-title": ["Highland Studies"]}


BOOK_ITEMS = [
    _item("Highland Studies", "10.1234/book", "book"),
    _item("Glacial valleys of the northern highlands", "10.1234/book.ch3", "book-chapter"),
    _item("Coastal erosion in the southern lowlands", "10.1234/book.ch4", "book-chapter"),
]

CHAPTER_PAGE = """Highland Studies
Glacial Valleys of the Northern Highlands
Ada Moreno
ISBN 978-1-55555-012-7
We map the glacial valleys of the northern highlands using field surveys
and elevation models, and compare their shapes across the region.
"""


def _isbn_meta(title, page=CHAPTER_PAGE):
    return PDFMetadata(title=title, title_source="font size", extracted_text=page,
                       isbn_candidates=[ISBN13])


def test_chapter_whose_title_is_the_page_title(client):
    client._make_request = MagicMock(return_value={"message": {"items": BOOK_ITEMS}})
    match = lookup_isbn(client, _isbn_meta("Glacial Valleys of the Northern Highlands"))
    assert (match.doi, match.score, match.isbn) == ("10.1234/book.ch3", 1.0, ISBN13)


def test_whole_book(client):
    cover = "Highland Studies\nEdited by Ada Moreno\nISBN 978-1-55555-012-7\n" + "x " * 60
    client._make_request = MagicMock(return_value={"message": {"items": BOOK_ITEMS}})
    assert lookup_isbn(client, _isbn_meta("Highland Studies", cover)).doi == "10.1234/book"


def test_no_record_with_the_page_title(client):
    client._make_request = MagicMock(return_value={"message": {"items": BOOK_ITEMS}})
    assert lookup_isbn(client, _isbn_meta("Tidal Flats of the Western Shore")) is None


def test_no_page_title_no_isbn_lookup(client):
    client._make_request = MagicMock()
    assert lookup_isbn(client, _isbn_meta(None)) is None
    client._make_request.assert_not_called()


def test_isbn_lookup_errors_fall_back_to_search(client):
    client._make_request = MagicMock(side_effect=CrossrefConnectionError("offline"))
    assert lookup_isbn(client, _isbn_meta("Glacial Valleys of the Northern Highlands")) is None


def test_book_record_with_its_subtitle_in_a_separate_field(client):
    """Crossref often files a book's subtitle apart; the page title has both."""
    book = dict(_item("Highland Studies", "10.1234/book", "monograph"),
                subtitle=["Glaciers, Valleys and Their Shapes"])
    cover = "Highland Studies: Glaciers, Valleys and Their Shapes\nISBN 978-1-55555-012-7\n" + "x " * 60
    client._make_request = MagicMock(return_value={"message": {"items": [book]}})
    meta = _isbn_meta("Highland Studies: Glaciers, Valleys and Their Shapes", cover)
    assert lookup_isbn(client, meta).doi == "10.1234/book"


def test_chapter_beats_a_monograph_of_the_same_title(client):
    items = [_item("Glacial valleys of the northern highlands", "10.1234/book", "monograph"),
             _item("Glacial valleys of the northern highlands", "10.1234/book.ch1", "book-chapter")]
    client._make_request = MagicMock(return_value={"message": {"items": items}})
    match = lookup_isbn(client, _isbn_meta("Glacial Valleys of the Northern Highlands"))
    assert match.doi == "10.1234/book.ch1"


@patch("papertag.core.crossref_client.requests.request")
def test_arxiv_records_typed_text_are_still_preprints(request, client):
    """Some arXiv DataCite records say resourceTypeGeneral "Text"."""
    record = {"data": {"attributes": dict(DATACITE_ARXIV["data"]["attributes"],
                                          types={"resourceTypeGeneral": "Text"})}}
    request.return_value = _response(200, record)
    assert client.fetch_metadata("10.48550/arXiv.2101.00001").is_preprint
