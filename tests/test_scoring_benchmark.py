"""Batch-mode outcomes on the recorded Crossref benchmark (offline).

The limits sit just past the results when step 3.2 landed (2026-11-26), so a
scoring change that makes batch mode accept more wrong matches, or fewer
right ones, fails here. Run ``python -m tests.scoring_benchmark`` for the table.

At step 3.2 (with derived variants, 553 queries): 389 right accepts, 2 wrong
(two records of the same study, once per variant), 20 of 485 accepts when the
right work is absent, nearly all duplicate records of the same work (preprint,
abstract, reprint). The old scoring: 222 right, 0 wrong, 13 of 485.
"""

import pytest

from tests.scoring_benchmark import (evaluate, load_cases, papertag_score, report,
                                     with_derived_variants)


@pytest.fixture(scope="module")
def stats():
    return evaluate(with_derived_variants(load_cases()), papertag_score())


def test_batch_accepts_most_answerable_queries(stats):
    assert stats["all"].accept_right >= 370, report(stats)


@pytest.mark.parametrize("variant", ["filename", "filename_us", "noisy", "text", "text_marked"])
def test_each_realistic_query_shape_is_accepted(stats, variant):
    """Underscore filenames and marked author lines once fell to near zero."""
    s = stats[variant]
    assert s.accept_right >= 0.9 * s.found, report(stats)


def test_batch_rarely_accepts_a_wrong_work(stats):
    assert stats["all"].accept_wrong <= 3, report(stats)


def test_batch_rarely_accepts_when_the_work_is_absent(stats):
    s = stats["all"]
    assert s.absent_accept <= 0.05 * s.absent_cases, report(stats)


@pytest.mark.parametrize("variant", ["title_only", "no_title"])
def test_single_kind_of_evidence_never_accepted(stats, variant):
    s = stats[variant]
    assert s.accept_right + s.accept_wrong == 0, report(stats)


def test_right_work_ranked_first(stats):
    s = stats["all"]
    assert s.top1 >= 0.94 * s.found, report(stats)
