"""Batch mode's accept/skip decision (step 3.5).

A top match is accepted only if it reaches the threshold and leads the next
rival by a margin: two near-equal candidates mean papertag can't tell which
paper the PDF is, and a wrong rename is worse than a skip.
"""

from unittest.mock import MagicMock, patch

import pytest

from papertag.cli import PDFMetadataManager, main, parse_arguments
from papertag.core.crossref_client import (BATCH_MARGIN, BATCH_THRESHOLD, CrossrefClient,
                                           CrossrefMatch, batch_choice)


def _match(doi: str, score: float, work_type: str = "journal-article") -> CrossrefMatch:
    return CrossrefMatch(doi=doi, title="Valleys of the north", authors=["Ada Moreno"],
                         year="2019", journal="J", score=score, work_type=work_type)


def test_defaults():
    assert BATCH_THRESHOLD == 0.80
    assert BATCH_MARGIN == 0.05


def test_accepts_a_clear_leader():
    top = _match("10.1234/a", 0.92)
    decision = batch_choice([top, _match("10.1234/b", 0.60)])
    assert decision.match is top


def test_accepts_a_single_candidate():
    top = _match("10.1234/a", 0.85)
    assert batch_choice([top]).match is top


def test_skips_below_threshold():
    decision = batch_choice([_match("10.1234/a", 0.79)])
    assert decision.match is None
    assert "0.79" in decision.reason and "0.80" in decision.reason


def test_skips_when_runner_up_is_close():
    decision = batch_choice([_match("10.1234/a", 0.90), _match("10.1234/b", 0.87)])
    assert decision.match is None
    assert "10.1234/b" in decision.reason


def test_runner_up_below_threshold_still_counts():
    """A 0.82 lead over 0.79 is no clearer than over 0.81."""
    assert batch_choice([_match("10.1234/a", 0.82), _match("10.1234/b", 0.79)]).match is None


def test_accepts_at_exactly_the_margin():
    top = _match("10.1234/a", 0.90)
    assert batch_choice([top, _match("10.1234/b", 0.85)]).match is top


def test_preprint_of_the_article_is_not_a_rival():
    """rank_key already chose between an article and its preprint."""
    top = _match("10.1234/a", 0.90)
    decision = batch_choice([top, _match("10.31234/osf.io/x", 0.90, "posted-content"),
                             _match("10.1234/c", 0.50)])
    assert decision.match is top


def test_article_is_not_a_rival_of_a_preprint_pick():
    top = _match("10.31234/osf.io/x", 0.90, "posted-content")
    assert batch_choice([top, _match("10.1234/a", 0.90)]).match is top


def test_rival_after_a_preprint_still_counts():
    decision = batch_choice([_match("10.1234/a", 0.90),
                             _match("10.31234/osf.io/x", 0.90, "posted-content"),
                             _match("10.1234/c", 0.88)])
    assert decision.match is None


def test_two_preprints_are_rivals():
    decision = batch_choice([_match("10.31234/osf.io/x", 0.90, "posted-content"),
                             _match("10.31234/osf.io/y", 0.89, "posted-content")])
    assert decision.match is None


def test_custom_threshold_and_margin():
    top = _match("10.1234/a", 0.70)
    assert batch_choice([top, _match("10.1234/b", 0.40)], threshold=0.65).match is top
    assert batch_choice([top, _match("10.1234/b", 0.60)], threshold=0.65, margin=0.2).match is None


def test_no_candidates():
    assert batch_choice([]).match is None


def test_search_records_the_work_type():
    client = CrossrefClient(email="test@example.invalid")
    item = {"DOI": "10.1234/a", "title": ["Valleys of the north"], "type": "posted-content",
            "author": [{"family": "Moreno", "given": "Ada"}], "issued": {"date-parts": [[2019]]}}
    with patch.object(client, "_make_request", return_value={"message": {"items": [item]}}):
        match, = client.search(title="Valleys of the north", author="Moreno", year="2019")
    assert match.work_type == "posted-content"


# --- command line -----------------------------------------------------------------

def test_threshold_flag_defaults_and_parses():
    assert parse_arguments(["a.pdf"]).threshold == BATCH_THRESHOLD
    assert parse_arguments(["a.pdf", "--threshold", "0.9"]).threshold == 0.9


@pytest.mark.parametrize("value", ["0", "1.5", "-0.1"])
def test_threshold_out_of_range_is_rejected(tmp_path, capsys, value):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"")
    assert main([str(pdf), "--email", "x@example.invalid", "--batch",
                 "--threshold", value]) == 1
    assert "--threshold" in capsys.readouterr().err


def test_threshold_reaches_the_manager(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"")
    with patch("papertag.cli.PDFMetadataManager") as manager:
        manager.return_value.process_files.return_value = False
        manager.return_value.stats = {"failed": 0}
        main([str(pdf), "--email", "x@example.invalid", "--batch", "--threshold", "0.9"])
    assert manager.call_args.kwargs["threshold"] == 0.9


def _batch_manager(matches, **kwargs):
    mgr = PDFMetadataManager(email="x@example.invalid", batch_mode=True, **kwargs)
    mgr.ui = MagicMock(quiet=True, verbose=False)
    mgr.pdf_processor = MagicMock()
    mgr.pdf_processor.extract_metadata.return_value = MagicMock(
        doi=None, title="Valleys of the north", authors="Ada Moreno", year="2019",
        used_ocr=False, title_source="font size", preprint=False)
    mgr.metadata_updater = MagicMock()
    mgr.metadata_updater.generate_zotero_filename.return_value = "new.pdf"
    mgr.metadata_updater.rename_file.return_value = "/tmp/new.pdf"
    mgr.crossref_client = MagicMock()
    mgr.crossref_client.search.return_value = matches
    mgr.logger = MagicMock()
    return mgr


def test_manager_skips_an_ambiguous_pick():
    mgr = _batch_manager([_match("10.1234/a", 0.90), _match("10.1234/b", 0.88)])
    assert mgr.process_single_pdf("/tmp/papertag_batch_test.pdf") == "skipped"
    mgr.metadata_updater.update_metadata.assert_not_called()
    reason = mgr.logger.log_skip.call_args[0][1]
    assert "10.1234/b" in reason


def test_manager_uses_its_threshold():
    mgr = _batch_manager([_match("10.1234/a", 0.85)], threshold=0.9)
    assert mgr.process_single_pdf("/tmp/papertag_batch_test.pdf") == "skipped"
    mgr = _batch_manager([_match("10.1234/a", 0.85)], threshold=0.7)
    assert mgr.process_single_pdf("/tmp/papertag_batch_test.pdf") == "completed"
