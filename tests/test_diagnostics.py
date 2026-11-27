"""Diagnostics go through `logging` to stderr; library modules never print,
and terminal output carries no emoji."""

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from papertag.cli import PDFMetadataManager, configure_logging
from papertag.core.metadata_updater import MetadataUpdate, MetadataUpdater
from papertag.core.pdf_processor import PDFNotFoundError, PDFProcessor
from papertag.ui import InteractiveUI
from papertag.utils.timestamp_utils import set_timestamps
from tests.pdf_fixtures import make_text_pdf

EMOJI = set("✓⚠❌⏳📝️")


def _has_emoji(text: str) -> bool:
    return any(ch in EMOJI for ch in text)


# --- library modules log, never print ---------------------------------------

def test_timestamp_warning_is_logged_not_printed(tmp_path, capsys, caplog):
    with caplog.at_level(logging.WARNING, logger="papertag"):
        set_timestamps(str(tmp_path / "missing.pdf"), {})
    assert capsys.readouterr().out == ""
    assert "missing.pdf" in caplog.text


def test_pdf_processor_details_are_debug_logs(tmp_path, capsys, caplog):
    path = make_text_pdf(tmp_path / "p.pdf",
                         ["A Study of Things", "Jane Doe", "doi:10.1234/abc.5678"])
    with caplog.at_level(logging.DEBUG, logger="papertag"):
        PDFProcessor(use_ocr=False).extract_metadata(str(path))
    assert capsys.readouterr().out == ""
    assert "10.1234/abc.5678" in caplog.text


def test_backup_notice_is_logged_not_printed(tmp_path, capsys, caplog):
    path = make_text_pdf(tmp_path / "p.pdf", ["Title"])
    with caplog.at_level(logging.INFO, logger="papertag"):
        MetadataUpdater(keep_backup=True).update_metadata(
            str(path), MetadataUpdate(title="T", authors="A"))
    assert capsys.readouterr().out == ""
    assert "p.pdf.bak" in caplog.text


# --- CLI wiring ------------------------------------------------------------

def test_configure_logging_writes_warnings_to_stderr(capsys):
    configure_logging(verbose=False, quiet=False)
    logging.getLogger("papertag.core.x").warning("disk is on fire")
    logging.getLogger("papertag.core.x").debug("hidden detail")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "disk is on fire" in captured.err
    assert "hidden detail" not in captured.err


def test_configure_logging_verbose_shows_debug(capsys):
    configure_logging(verbose=True, quiet=False)
    logging.getLogger("papertag.core.x").debug("visible detail")
    assert "visible detail" in capsys.readouterr().err


def test_configure_logging_is_idempotent(capsys):
    configure_logging(verbose=False, quiet=False)
    configure_logging(verbose=False, quiet=False)
    logging.getLogger("papertag").warning("once")
    assert capsys.readouterr().err.count("once") == 1


def test_file_error_goes_to_stderr_without_emoji(capsys):
    configure_logging(verbose=False, quiet=False)  # as main() does
    mgr = PDFMetadataManager(email="x@y.com", batch_mode=True)
    mgr.pdf_processor = MagicMock()
    mgr.pdf_processor.extract_metadata.side_effect = PDFNotFoundError("gone.pdf missing")
    mgr.logger = MagicMock()

    assert mgr.process_single_pdf("/tmp/gone.pdf") == "failed"

    captured = capsys.readouterr()
    assert "gone.pdf missing" in captured.err
    assert "gone.pdf missing" not in captured.out
    assert not _has_emoji(captured.out + captured.err)


# --- UI output ---------------------------------------------------------------

def test_summary_and_progress_have_no_emoji(capsys):
    ui = InteractiveUI()
    ui.show_progress(current=1, total=3, completed=1, skipped=0, failed=0,
                     current_file="a.pdf")
    ui.print_summary(total=3, completed=1, skipped=1, failed=1, log_path=None)
    assert not _has_emoji(capsys.readouterr().out)


def test_pypdf_parser_chatter_hidden_unless_verbose(tmp_path, capsys):
    """pypdf logs its own parse warnings; we already report the failure, so
    they are noise except when debugging."""
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")

    configure_logging(verbose=False, quiet=False)
    with pytest.raises(Exception):
        PDFProcessor(use_ocr=False)._extract_text(str(broken))
    assert "invalid pdf header" not in capsys.readouterr().err

    configure_logging(verbose=True, quiet=False)
    with pytest.raises(Exception):
        PDFProcessor(use_ocr=False)._extract_text(str(broken))
    assert "invalid pdf header" in capsys.readouterr().err


def test_malformed_xmp_is_quiet_unless_verbose(tmp_path, capsys):
    """pikepdf recovers from a bad XMP packet but logs a traceback; that is
    noise for the user, like pypdf's parser warnings."""
    path = make_text_pdf(tmp_path / "bad-xmp.pdf", ["Some text on the page"] * 10,
                         xmp='<x:xmpmeta xmlns:x="adobe:ns:meta/"><foo/></x:xmpmeta>')

    configure_logging(verbose=False, quiet=False)
    PDFProcessor(use_ocr=False).extract_metadata(str(path))
    assert "XMP" not in capsys.readouterr().err

    configure_logging(verbose=True, quiet=False)
    PDFProcessor(use_ocr=False).extract_metadata(str(path))
    assert "XMP" in capsys.readouterr().err


def test_stdout_is_flushed_before_a_diagnostic(monkeypatch):
    """Piped stdout is block-buffered; without a flush a file's error shows
    up on stderr ahead of that file's own header."""
    events = []

    class Recorder:
        def __init__(self, name):
            self.name = name

        def write(self, text):
            events.append((self.name, text))

        def flush(self):
            events.append((self.name, "<flush>"))

    monkeypatch.setattr("sys.stdout", Recorder("out"))
    monkeypatch.setattr("sys.stderr", Recorder("err"))
    configure_logging(verbose=False, quiet=False)
    print("Processing: a.pdf")
    logging.getLogger("papertag.cli").error("a.pdf: broken")

    first_err = next(i for i, (name, _) in enumerate(events) if name == "err")
    assert ("out", "<flush>") in events[:first_err]
