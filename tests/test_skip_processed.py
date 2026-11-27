"""Files an earlier papertag run tagged are skipped unless --force."""

from unittest.mock import MagicMock

from papertag.cli import PDFMetadataManager, parse_arguments
from papertag.core import CrossrefMatch, PDFProcessor
from papertag.core.metadata_updater import MetadataUpdate, MetadataUpdater
from tests.pdf_fixtures import make_text_pdf

MATCH = CrossrefMatch(doi="10.1234/rivers.2019.07", title="Sediment Transport in Braided Rivers",
                      authors=["Ada Moreno"], year="2019", journal="Journal of Rivers",
                      score=1.0)


def _tagged_pdf(tmp_path):
    path = make_text_pdf(tmp_path / "paper.pdf", ["Sediment Transport in Braided Rivers"])
    MetadataUpdater().update_metadata(str(path), MetadataUpdate(
        title=MATCH.title, authors="Ada Moreno", year="2019",
        journal=MATCH.journal, doi=MATCH.doi))
    return path


def _manager(**kwargs):
    mgr = PDFMetadataManager(batch_mode=True, quiet=True, rename=False, **kwargs)
    mgr.crossref_client = MagicMock()
    mgr.crossref_client.fetch_metadata.return_value = MATCH
    mgr.crossref_client.search.return_value = [MATCH]
    mgr.crossref_client.shown_on_page.return_value = True
    mgr.logger = MagicMock()
    return mgr


def test_detects_papertag_output(tmp_path):
    assert PDFProcessor.tagged_by_papertag(str(_tagged_pdf(tmp_path)))


def test_untouched_pdf_is_not_tagged(tmp_path):
    path = make_text_pdf(tmp_path / "p.pdf", ["Some title"],
                         docinfo={"/Subject": "Journal of Rivers, vol. 3"})
    assert not PDFProcessor.tagged_by_papertag(str(path))


def test_unreadable_pdf_is_not_tagged(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a pdf")
    assert not PDFProcessor.tagged_by_papertag(str(path))


def test_processed_file_is_skipped(tmp_path):
    mgr = _manager()
    path = _tagged_pdf(tmp_path)

    assert mgr.process_single_pdf(str(path)) == "skipped"
    reason = mgr.logger.log_skip.call_args.args[1]
    assert "--force" in reason
    mgr.crossref_client.search.assert_not_called()
    mgr.crossref_client.fetch_metadata.assert_not_called()


def test_force_processes_it_again(tmp_path):
    mgr = _manager(force=True)
    assert mgr.process_single_pdf(str(_tagged_pdf(tmp_path))) == "completed"


def test_force_flag_parses():
    assert parse_arguments(["a.pdf", "--force"]).force
    assert parse_arguments(["a.pdf", "-f"]).force
    assert not parse_arguments(["a.pdf"]).force
