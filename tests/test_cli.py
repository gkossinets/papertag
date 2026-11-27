"""Tests for the command-line entry point: argument parsing, file
collection, and exit codes."""

from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from papertag import __version__
from papertag.cli import collect_pdf_files, main, parse_arguments


def _make_pdfs(directory: Path, names: List[str]) -> List[str]:
    """Create empty placeholder .pdf files and return their paths."""
    paths = []
    for name in names:
        path = directory / name
        path.write_bytes(b"")
        paths.append(str(path))
    return paths


def test_parse_accepts_multiple_inputs():
    """Shell-expanded globs arrive as several positional arguments."""
    args = parse_arguments(["a.pdf", "b.pdf", "c.pdf"])
    assert args.inputs == ["a.pdf", "b.pdf", "c.pdf"]


def test_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_arguments(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_collect_merges_files_and_directories(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    a, = _make_pdfs(tmp_path, ["a.pdf"])
    b, c = _make_pdfs(sub, ["b.pdf", "c.PDF"])
    _make_pdfs(sub, ["notes.txt"])

    files = collect_pdf_files([a, str(sub)])

    assert files == sorted([a, b, c])


def test_collect_deduplicates_overlapping_inputs(tmp_path):
    a, = _make_pdfs(tmp_path, ["a.pdf"])
    assert collect_pdf_files([a, str(tmp_path), a]) == [a]


def _run_main(argv: List[str], outcome) -> int:
    """Run main() with process_single_pdf replaced by `outcome`."""
    with patch("papertag.cli.PDFMetadataManager.process_single_pdf",
               side_effect=outcome):
        return main(argv)


def test_exit_zero_when_all_files_succeed(tmp_path):
    files = _make_pdfs(tmp_path, ["a.pdf", "b.pdf"])
    code = _run_main(files + ["--from-filename", "--quiet"],
                     lambda path: "completed")
    assert code == 0


def test_exit_zero_when_files_only_skipped(tmp_path):
    files = _make_pdfs(tmp_path, ["a.pdf"])
    code = _run_main(files + ["--from-filename", "--quiet"],
                     lambda path: "skipped")
    assert code == 0


def test_exit_one_when_any_file_fails(tmp_path):
    files = _make_pdfs(tmp_path, ["a.pdf", "b.pdf"])
    outcomes = iter(["completed", "failed"])
    code = _run_main(files + ["--from-filename", "--quiet"],
                     lambda path: next(outcomes))
    assert code == 1


def test_exit_130_on_interrupt(tmp_path):
    files = _make_pdfs(tmp_path, ["a.pdf", "b.pdf"])

    def interrupt(path):
        raise KeyboardInterrupt

    code = _run_main(files + ["--from-filename", "--quiet"], interrupt)
    assert code == 130


def test_exit_one_when_input_missing(tmp_path, capsys):
    code = main([str(tmp_path / "missing.pdf"), "--from-filename"])
    assert code == 1
    assert "does not exist" in capsys.readouterr().err


def test_exit_one_when_no_pdfs_found(tmp_path, capsys):
    code = main([str(tmp_path), "--from-filename"])
    assert code == 1
    assert "No PDF files found" in capsys.readouterr().err


# --- the Crossref email is optional -------------------------------------------

def test_runs_without_email(tmp_path, monkeypatch):
    """No --email and no CROSSREF_EMAIL: search Crossref's public pool."""
    monkeypatch.delenv("CROSSREF_EMAIL", raising=False)
    files = _make_pdfs(tmp_path, ["a.pdf"])
    assert _run_main(files + ["--quiet"], lambda path: "completed") == 0


def test_manager_without_email_still_has_a_crossref_client():
    from papertag.cli import PDFMetadataManager
    assert PDFMetadataManager(email=None).crossref_client is not None


def test_client_without_email_sends_no_mailto():
    from papertag.core import CrossrefClient
    client = CrossrefClient()
    assert "mailto" not in client.headers["User-Agent"]
    assert client.headers["User-Agent"].startswith("papertag/")


def test_client_with_email_sends_mailto():
    from papertag.core import CrossrefClient
    agent = CrossrefClient(email="me@example.invalid").headers["User-Agent"]
    assert "mailto:me@example.invalid" in agent
