"""Side-by-side current vs new metadata, and --dry-run (change nothing)."""

from unittest.mock import MagicMock, patch

from papertag.cli import PDFMetadataManager, main, parse_arguments
from papertag.core import CrossrefMatch
from papertag.core.metadata_updater import MetadataUpdate
from papertag.ui import InteractiveUI
from tests.pdf_fixtures import make_text_pdf

MATCH = CrossrefMatch(doi="10.1234/rivers.2019.07", title="Sediment Transport in Braided Rivers",
                      authors=["Ada Moreno"], year="2019", journal="Journal of Rivers",
                      score=0.95)
NEW = MetadataUpdate(title=MATCH.title, authors="Ada Moreno", year="2019",
                     journal="Journal of Rivers", doi=MATCH.doi)


# --- the comparison table ------------------------------------------------------

def _rows(output):
    return {line.lstrip("* ").split()[0]: line for line in output.splitlines() if line.strip()}


def test_show_changes_puts_current_and_new_on_one_row(capsys):
    current = MetadataUpdate(title="Microsoft Word - draft3.docx", authors="")
    InteractiveUI().show_changes(current, NEW, "scan0042.pdf",
                                 "Moreno - 2019 - Sediment Transport in Braided Rivers.pdf")
    rows = _rows(capsys.readouterr().out)
    assert "draft3.docx" in rows["Title"] and "Sediment Transport" in rows["Title"]
    assert "(none)" in rows["Authors"] and "Ada Moreno" in rows["Authors"]
    assert "scan0042.pdf" in rows["Filename"] and "Moreno - 2019" in rows["Filename"]


def test_show_changes_marks_only_changed_rows(capsys):
    current = MetadataUpdate(title=MATCH.title, authors="Someone Else", year="2019")
    InteractiveUI().show_changes(current, NEW, "a.pdf", "a.pdf")
    out = capsys.readouterr().out
    marked = {line.split()[1] for line in out.splitlines() if line.startswith("*")}
    assert marked == {"Authors", "Journal", "DOI"}


def test_show_changes_fits_the_terminal(capsys):
    long_title = "A " * 200
    with patch("shutil.get_terminal_size", return_value=__import__("os").terminal_size((80, 24))):
        InteractiveUI().show_changes(MetadataUpdate(title=long_title, authors=""),
                                     NEW, "a.pdf", "b.pdf")
    assert all(len(line) <= 80 for line in capsys.readouterr().out.splitlines())


def test_show_changes_omits_isbn_when_neither_has_one(capsys):
    InteractiveUI().show_changes(MetadataUpdate(title="", authors=""), NEW, "a.pdf", "b.pdf")
    assert "ISBN" not in capsys.readouterr().out


def test_confirm_shows_current_values(capsys):
    current = MetadataUpdate(title="Untitled scan", authors="")
    with patch("builtins.input", return_value="a"):
        assert InteractiveUI().confirm_metadata("a.pdf", NEW, "b.pdf", current=current)
    assert "Untitled scan" in capsys.readouterr().out


def test_quiet_ui_shows_no_table(capsys):
    InteractiveUI(quiet=True).show_changes(MetadataUpdate(title="", authors=""),
                                           NEW, "a.pdf", "b.pdf")
    assert capsys.readouterr().out == ""


# --- --dry-run -------------------------------------------------------------------

def _manager(**kwargs):
    mgr = PDFMetadataManager(batch_mode=True, dry_run=True, **kwargs)
    mgr.crossref_client = MagicMock()
    mgr.crossref_client.search.return_value = [MATCH]
    mgr.logger = MagicMock()
    return mgr


def test_dry_run_changes_nothing(tmp_path, capsys):
    path = make_text_pdf(tmp_path / "scan0042.pdf", ["Sediment Transport in Braided Rivers"])
    before = path.read_bytes()

    assert _manager().process_single_pdf(str(path)) == "completed"

    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["scan0042.pdf"]
    out = capsys.readouterr().out
    assert "Dry run" in out
    assert "Moreno - 2019 - Sediment Transport in Braided Rivers.pdf" in out


def test_dry_run_is_logged_as_not_applied(tmp_path):
    path = make_text_pdf(tmp_path / "scan0042.pdf", ["Sediment Transport in Braided Rivers"])
    mgr = _manager()
    mgr.process_single_pdf(str(path))
    mgr.logger.log_success.assert_not_called()
    planned = mgr.logger.log_planned.call_args.kwargs
    assert planned["doi"] == MATCH.doi
    assert planned["new_path"].endswith("Moreno - 2019 - Sediment Transport in Braided Rivers.pdf")


def test_dry_run_from_filename_changes_nothing(tmp_path):
    path = make_text_pdf(tmp_path / "Moreno - 2019 - Sediment Transport.pdf", ["x"])
    before = path.read_bytes()
    mgr = PDFMetadataManager(batch_mode=True, dry_run=True, from_filename=True, quiet=True)
    mgr.logger = MagicMock()

    assert mgr.process_single_pdf(str(path)) == "completed"
    assert path.read_bytes() == before
    mgr.logger.log_planned.assert_called_once()


def test_dry_run_summary_says_nothing_was_written(tmp_path, capsys):
    path = make_text_pdf(tmp_path / "Moreno - 2019 - Sediment Transport.pdf", ["x"])
    assert main([str(path), "--from-filename", "--batch", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Would update:" in out
    assert "nothing was written" in out.lower()


def test_dry_run_flag_parses():
    assert parse_arguments(["a.pdf", "--dry-run"]).dry_run
    assert parse_arguments(["a.pdf", "-n"]).dry_run
