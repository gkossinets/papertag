"""Article vs preprint on a score tie: follow what the PDF is (user's choice, 2026-11-26).

Crossref often has both a preprint (posted-content) and the published article
for one work, and papertag scores them the same. A publisher PDF should get
the article's record; a preprint PDF its own.
"""

from unittest.mock import patch

import pytest

from papertag.cli import search_query
from papertag.core import FilenameHints, PDFMetadata, PDFProcessor
from papertag.core.crossref_client import CrossrefClient
from tests.pdf_fixtures import make_text_pdf

BODY = ["Wells in the lower basin were sampled monthly over three dry seasons."] * 6
NO_HINTS = FilenameHints(author=None, year=None, title=None, confidence=0.0)


@pytest.mark.parametrize("marker", [
    ("arXiv:2601.00001v2 [physics.geo-ph] 5 Jan 2026", 20, "rotated"),   # arXiv's margin stamp
    "bioRxiv preprint doi: https://doi.org/10.1101/2026.01.05.000001",
    "medRxiv preprint doi: https://doi.org/10.1101/2026.01.05.000001",
    "This preprint has not been peer reviewed.",
    "Electronic copy available at: https://ssrn.com/abstract=1234567",
    "Preprint as of February 2, 2026",
    "Preprint submitted to Journal of Valley Studies",        # Elsevier's elsarticle footer
    "DOI: https://doi.org/10.31234/osf.io/abcde",             # PsyArXiv / OSF Preprints
    "DOI: https://doi.org/10.21203/rs.3.rs-123456/v1",        # Research Square
])
def test_preprint_markers(tmp_path, marker):
    path = make_text_pdf(tmp_path / "p.pdf", [marker, "Groundwater Recharge in Dry Valleys", *BODY])
    assert PDFProcessor(use_ocr=False).extract_metadata(str(path)).preprint is True


@pytest.mark.parametrize("line", [
    # A published paper citing preprints on its first page.
    "[3] L. Park, Sparse soil models, arXiv preprint arXiv:2101.00001 (2021).",
    "Preprints of this work were presented at the Valley Hydrology Meeting.",
    "Research increasingly makes its way to preprint servers.",
    "Data are available at https://osf.io/abcde/.",
])
def test_citing_a_preprint_is_not_being_one(tmp_path, line):
    path = make_text_pdf(tmp_path / "p.pdf", ["Groundwater Recharge in Dry Valleys", *BODY, line])
    assert PDFProcessor(use_ocr=False).extract_metadata(str(path)).preprint is False


def test_query_carries_the_preprint_flag():
    assert search_query(PDFMetadata(preprint=True), NO_HINTS).preprint is True
    assert search_query(PDFMetadata(), NO_HINTS).preprint is False


def _items():
    def item(doi, kind):
        return {"DOI": doi, "type": kind, "title": ["Groundwater Recharge in Dry Valleys"],
                "author": [{"given": "Ana", "family": "Moreno"}], "issued": {"date-parts": [[2019]]}}
    return [item("10.1234/valleys.2019.1", "journal-article"),
            item("10.31234/osf.io/abcde", "posted-content")]


@pytest.mark.parametrize("preprint, winner", [
    (False, "10.1234/valleys.2019.1"),
    (True, "10.31234/osf.io/abcde"),
])
def test_tie_goes_to_the_record_matching_the_pdf(preprint, winner):
    client = CrossrefClient(email="")
    for items in (_items(), _items()[::-1]):   # whatever order Crossref returns
        with patch.object(client, "_make_request", return_value={"message": {"items": items}}):
            matches = client.search(title="Groundwater Recharge in Dry Valleys", author="Moreno",
                                    year="2019", prefer_preprint=preprint)
        assert matches[0].doi == winner
