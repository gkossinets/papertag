"""How papertag turns its evidence into a Crossref search request (roadmap step 3.4)."""

import pytest

from papertag.core.crossref_client import CrossrefClient


@pytest.mark.parametrize("author, surname", [
    ("Moreno", "Moreno"),
    ("Ana Moreno", "Moreno"),
    ("Moreno, Ana", "Moreno"),
    ("Moreno, A., Park, L.", "Moreno"),
    # Zotero-style filenames: "First et al." and "First and Second".
    ("Moreno et al.", "Moreno"),
    ("Moreno et al", "Moreno"),
    ("Moreno and Park", "Moreno"),
    ("Moreno & Park", "Moreno"),
    ("Silva-Moreno and Park", "Silva-Moreno"),
    # A page author line, with affiliation markers.
    ("Ana Moreno, Lee Park", "Moreno"),
    ("Ana Moreno1, Lee Park2*", "Moreno"),
    ("Ana Moreno1,2 and Lee Park3", "Moreno"),
    ("José García-López; Ana Moreno", "García-López"),
    ("Ana Moreno · Lee Park", "Moreno"),
    # Initials are never the surname: "A. Moreno", Vancouver-style "Moreno A".
    ("A. Moreno", "Moreno"),
    ("Moreno A, Park L", "Moreno"),
])
def test_first_author_surname(author, surname):
    assert CrossrefClient.first_author_surname(author) == surname


@pytest.mark.parametrize("author", ["", "   ", "et al.", "1, 2"])
def test_no_surname(author):
    assert CrossrefClient.first_author_surname(author) is None


def params(url):
    from urllib.parse import parse_qs, urlsplit
    split = urlsplit(url)
    assert split.netloc == "api.crossref.org" and split.path == "/works"
    return {k: v[0] for k, v in parse_qs(split.query).items()}


def test_fielded_query():
    # query.bibliographic ranks by title and year as a citation would;
    # query.author matches contributor names only.
    p = params(CrossrefClient.search_url("Groundwater Recharge in Dry Valleys", "Moreno et al.", "2019", rows=10))
    assert p["query.bibliographic"] == "Groundwater Recharge in Dry Valleys 2019"
    assert p["query.author"] == "Moreno"
    assert p["rows"] == "10"
    assert "query" not in p


def test_select_has_every_field_scoring_and_display_read():
    fields = set(params(CrossrefClient.search_url("Groundwater Recharge", None, None, rows=10))["select"].split(","))
    assert {"DOI", "title", "subtitle", "author", "issued", "published-print",
            "published-online", "created", "container-title"} <= fields


def test_all_caps_title_is_sent_in_title_case():
    p = params(CrossrefClient.search_url("GROUNDWATER RECHARGE IN DRY VALLEYS", None, None, rows=10))
    assert p["query.bibliographic"] == "Groundwater Recharge In Dry Valleys"


def test_author_and_year_without_title():
    p = params(CrossrefClient.search_url(None, "Moreno", "2019", rows=10))
    assert p["query.bibliographic"] == "2019"
    assert p["query.author"] == "Moreno"


def test_author_without_a_name_is_left_out():
    p = params(CrossrefClient.search_url("Groundwater Recharge", "et al.", None, rows=10))
    assert "query.author" not in p


def test_nothing_to_search():
    assert CrossrefClient.search_url(None, None, None, rows=10) is None
    assert CrossrefClient.search_url(None, "et al.", None, rows=10) is None


def test_search_sends_the_fielded_query():
    from unittest.mock import patch
    client = CrossrefClient(email="")
    with patch.object(client, "_make_request", return_value={"message": {"items": []}}) as request:
        client.search(title="Groundwater Recharge", author="Moreno and Park", year="2019", max_results=5)
    p = params(request.call_args.args[0])
    assert p["query.author"] == "Moreno"
    assert p["rows"] == "10"


def test_unreliable_author_is_not_a_field_query():
    # query.author nearly filters: a junk "surname" from a page-text guess
    # ("University", "of") pushed the right paper out of the results.
    p = params(CrossrefClient.search_url("Groundwater Recharge", "Ana Moreno", "2019", rows=10,
                                         author_reliable=False))
    assert p["query.bibliographic"] == "Groundwater Recharge Moreno 2019"
    assert "query.author" not in p


def _item(doi, kind):
    return {"DOI": doi, "type": kind, "title": ["Groundwater Recharge in Dry Valleys"],
            "author": [{"given": "Ana", "family": "Moreno"}],
            "issued": {"date-parts": [[2019]]}}


def test_published_record_beats_its_preprint_on_a_tie():
    from unittest.mock import patch
    client = CrossrefClient(email="")
    items = [_item("10.31234/osf.io/abcde", "posted-content"),
             _item("10.1234/valleys.2019.1", "journal-article")]
    with patch.object(client, "_make_request", return_value={"message": {"items": items}}):
        matches = client.search(title="Groundwater Recharge in Dry Valleys", author="Moreno", year="2019")
    assert matches[0].score == matches[1].score
    assert matches[0].doi == "10.1234/valleys.2019.1"


def test_better_scoring_preprint_still_wins():
    from unittest.mock import patch
    client = CrossrefClient(email="")
    other = _item("10.1234/other.2019.1", "journal-article")
    other["title"] = ["Surface Runoff in Coastal Plains"]
    items = [_item("10.31234/osf.io/abcde", "posted-content"), other]
    with patch.object(client, "_make_request", return_value={"message": {"items": items}}):
        matches = client.search(title="Groundwater Recharge in Dry Valleys", author="Moreno", year="2019")
    assert matches[0].doi == "10.31234/osf.io/abcde"
