"""Match scoring (roadmap step 3.2): normalize by available evidence,
token-based title similarity, whole-word author match.

Batch mode auto-accepts the top match at >= 0.80, so the tests state which
evidence may and may not reach that line.
"""

import pytest

from papertag.core.crossref_client import CrossrefClient

BATCH = 0.80

TITLE = "Groundwater Recharge Estimation in Semi-Arid Basins of Southern Europe"


def item(title=TITLE, family="Moreno", given="Ana", year=2019, subtitle=None, authors=None):
    record = {"title": [title], "issued": {"date-parts": [[year]]}}
    if subtitle:
        record["subtitle"] = [subtitle]
    record["author"] = authors if authors is not None else [{"given": given, "family": family}]
    return record


@pytest.fixture
def client():
    return CrossrefClient(email="test@example.com")


def score(client, record, title=None, author=None, year=None):
    return client._calculate_match_score(record, title=title, author=author, year=year)


# --- normalization by available evidence ---------------------------------------

def test_full_evidence_scores_one(client):
    assert score(client, item(), TITLE, "Moreno", "2019") == pytest.approx(1.0)


def test_title_and_year_without_author_can_be_accepted(client):
    """Before, a missing author capped the score at 0.7: batch never accepted."""
    assert score(client, item(), TITLE, None, "2019") >= BATCH


def test_title_and_author_without_year_can_be_accepted(client):
    assert score(client, item(), TITLE, "Moreno", None) >= BATCH


def test_title_alone_is_never_batch_accepted(client):
    """One kind of evidence can't tell a paper from others with a similar or
    generic title ("Introduction", "Book Review")."""
    s = score(client, item(), TITLE)
    assert s < BATCH
    assert s >= 0.65  # still shown as a medium-confidence suggestion


def test_no_title_is_low_confidence(client):
    """Author and year alone match every paper that author wrote that year."""
    assert score(client, item(), None, "Moreno", "2019") < 0.65


def test_wrong_author_blocks_batch(client):
    assert score(client, item(), TITLE, "Fischer", "2019") < BATCH


def test_wrong_year_blocks_batch(client):
    assert score(client, item(), TITLE, "Moreno", "2005") < BATCH


def test_adjacent_year_is_partial_credit(client):
    exact = score(client, item(), TITLE, "Moreno", "2019")
    adjacent = score(client, item(), TITLE, "Moreno", "2018")
    far = score(client, item(), TITLE, "Moreno", "2005")
    assert exact > adjacent > far


def test_score_is_capped_at_one(client):
    assert score(client, item(), TITLE, "Ana Moreno", "2019") <= 1.0


# --- title similarity ----------------------------------------------------------

def sim(client, a, b):
    return client._fuzzy_title_similarity(a, b)


def test_identical_titles(client):
    assert sim(client, TITLE, TITLE) == pytest.approx(1.0)


def test_case_punctuation_and_accents_ignored(client):
    assert sim(client, "Écologie des Sols: Une Revue", "ecologie des sols une revue") == pytest.approx(1.0)


def test_word_order_matters_little(client):
    assert sim(client, "Semi-Arid Basins of Southern Europe: Groundwater Recharge Estimation",
               TITLE) > 0.9


def test_unrelated_titles_sharing_words_score_low(client):
    """SequenceMatcher on characters gave unrelated titles 0.3-0.5."""
    assert sim(client, "Groundwater Quality in Coastal Aquifers",
               "Deep Learning for Weather Prediction") < 0.2
    assert sim(client, "Machine Learning in Climate",
               "Deep Learning for Weather Prediction") < 0.4


def test_first_line_of_wrapped_title_matches(client):
    """Page text often yields only the first line of a two-line title."""
    assert sim(client, "Groundwater Recharge Estimation in Semi-Arid", TITLE) >= 0.85


def test_short_generic_query_does_not_match_long_title(client):
    assert sim(client, "Introduction", "Introduction to Groundwater Hydrology and Modelling") < 0.5


def test_page_text_damage_tolerated(client):
    """Caps, a line-break hyphen, a dropped letter, a ligature."""
    noisy = "GROUNDWATER RECH- ARGE ESTIMATON IN SEMI-ARID BASINS OF SOUTHERN EUROPE"
    assert sim(client, noisy, TITLE) >= 0.85
    assert sim(client, "Eﬃcient Groundwater Modelling", "Efficient Groundwater Modelling") == pytest.approx(1.0)


def test_subtitle_is_considered(client):
    """PDFs print "Title: Subtitle"; Crossref may hold the subtitle separately."""
    record = item(title="Groundwater Recharge Estimation", subtitle="A Review of Field Methods")
    s = score(client, record, "Groundwater Recharge Estimation: A Review of Field Methods",
              "Moreno", "2019")
    assert s >= 0.95


# --- author matching -----------------------------------------------------------

def author_credit(client, query, family, given="X"):
    """Score difference an author query makes (title and year held fixed)."""
    record = item(family=family, given=given)
    with_author = score(client, record, TITLE, query, "2019")
    return with_author >= 0.95


@pytest.mark.parametrize("query,family", [
    ("Moreno", "Moreno"),
    ("MORENO", "Moreno"),
    ("Moreno, A.", "Moreno"),
    ("A. Moreno and B. Fischer", "Fischer"),
    ("Ana Moreno; Bernd Fischer", "Fischer"),
    ("Moreno et al.", "Moreno"),
    ("Müller", "Muller"),
    ("Muller", "Müller"),
    ("J. van der Berg", "van der Berg"),
    ("O'Neill", "O’Neill"),
    ("Li", "Li"),
    # Page-text author lines carry affiliation markers.
    ("John Moreno1, Ana Lopez2", "Moreno"),
    ("Moreno¹ and Lopez²", "Lopez"),
    ("Moreno*, Lopez†", "Lopez"),
    # Hints often carry only the main part of a compound surname.
    ("Berg", "van der Berg"),
    ("Smith", "Smith-Jones"),
    ("García", "García-López"),
])
def test_author_matches(client, query, family):
    assert author_credit(client, query, family)


@pytest.mark.parametrize("query,family", [
    ("Lipton", "Li"),          # "Li" must not match inside "Lipton"
    ("Li", "Lipton"),
    ("Anders", "Andersen"),
    ("Van", "van der Berg"),   # a particle alone is not a surname
])
def test_author_does_not_match_substrings(client, query, family):
    assert not author_credit(client, query, family)


def test_author_match_ignores_given_names(client):
    """A query line "Moreno Fischer" must not credit an author whose given name is Moreno."""
    assert not author_credit(client, "Moreno", family="Fischer", given="Moreno")


def test_item_without_authors_gives_no_author_credit(client):
    record = item(authors=[])
    assert score(client, record, TITLE, "Moreno", "2019") < BATCH


# --- a near-identical title is required for batch --------------------------------
# Benchmark (tests/data): right answers have title similarity >= 0.87; papers by
# the same authors with similar titles reached 0.80 overall at 0.64-0.83.

def test_similar_title_same_author_and_year_is_not_batch_accepted(client):
    """A different paper by the same people, one keyword apart."""
    query = "Comparison of Biliary Complications Between Living and Deceased Donor Liver Transplantations"
    other = item(title="Comparison of Vascular Complications Between Living and Deceased "
                       "Donor Liver Transplantations in Adults")
    assert score(client, other, query, "Moreno", "2019") < BATCH


def test_truncated_title_with_author_is_batch_accepted(client):
    """The page-text case: first line of the title, the author line, no year."""
    assert score(client, item(), "Groundwater Recharge Estimation in Semi-Arid",
                 "Ana Moreno, Bernd Fischer", None) >= BATCH


# --- review findings (step 3.2) ---------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("supervised", "unsupervised"), ("male", "female"), ("linear", "nonlinear"),
    ("hypertension", "hypotension"), ("intracellular", "intercellular"),
    ("preoperative", "postoperative"), ("increasing", "decreasing"),
    ("biliary", "ciliary"), ("1980s", "1990s"),
])
def test_opposite_or_different_words_are_not_fuzzy_matches(client, a, b):
    assert not client._words_match(a, b)


@pytest.mark.parametrize("a,b", [
    ("estimaton", "estimation"),     # dropped letter
    ("estimatoin", "estimation"),    # transposition
    ("groundwatar", "groundwater"),  # substitution
])
def test_page_text_damage_is_a_fuzzy_match(client, a, b):
    assert client._words_match(a, b)


def test_companion_paper_with_opposite_word_ranks_below_right_one(client):
    right = item(title="Supervised Learning of Sparse Feature Representations")
    other = item(title="Unsupervised Learning of Sparse Feature Representations")
    query = ("Supervised Learning of Sparse Feature Representations", "Moreno", "2019")
    assert score(client, right, *query) > score(client, other, *query)
    assert score(client, other, *query) < BATCH


def test_exact_title_beats_fuzzy_title(client):
    exact = item(title="Groundwater Recharge Estimation")
    damaged = item(title="Groundwater Recharge Estimaton")
    query = ("Groundwater Recharge Estimation", "Moreno", "2019")
    assert score(client, exact, *query) > score(client, damaged, *query)


def test_underscore_filename_title(client):
    """parse_filename leaves underscores in titles from Author_Year_Title.pdf."""
    assert sim(client, "Groundwater_Recharge_Estimation_in_Semi-Arid_Basins_of_Southern_Europe",
               TITLE) == pytest.approx(1.0)


@pytest.mark.parametrize("crossref_title,query", [
    ("Photocatalytic activity of TiO<sub>2</sub> thin films",
     "Photocatalytic activity of TiO2 thin films"),
    ("Effects of elevated CO<sub>2</sub> on wheat yield",
     "Effects of elevated CO2 on wheat yield"),
    ("Growth rings of <i>Quercus robur</i> in dry summers",
     "Growth rings of Quercus robur in dry summers"),
    ("Soil &amp; water conservation in terraced fields",
     "Soil & water conservation in terraced fields"),
])
def test_markup_in_crossref_titles_is_ignored(client, crossref_title, query):
    assert sim(client, query, crossref_title) == pytest.approx(1.0)
