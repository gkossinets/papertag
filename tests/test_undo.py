"""--undo LOG: put back the metadata and filenames a logged run changed."""

import json
import os

import pikepdf
import pytest

from papertag.cli import main, parse_arguments
from papertag.core.metadata_updater import MetadataUpdate, MetadataUpdater
from tests.pdf_fixtures import make_text_pdf, xmp_packet

ORIGINAL_DOCINFO = {"/Title": "Microsoft Word - draft3.docx", "/Author": "amoreno",
                    "/Producer": "Some Writer 2.1", "/CustomTag": "keep me"}
ORIGINAL_XMP = xmp_packet('xmlns:dc="http://purl.org/dc/elements/1.1/">'
                          '<dc:format>application/pdf</dc:format>')
FILENAME = "Moreno - 2019 - Sediment Transport in Braided Rivers.pdf"


def _docinfo(path):
    with pikepdf.open(path) as pdf:
        return {str(k): str(v) for k, v in pdf.docinfo.items()}


def _xmp_bytes(path):
    with pikepdf.open(path) as pdf:
        stream = pdf.Root.get("/Metadata")
        return None if stream is None else stream.read_bytes()


# --- snapshot / restore --------------------------------------------------------

def test_snapshot_restore_round_trip(tmp_path):
    path = make_text_pdf(tmp_path / "p.pdf", ["x"], docinfo=ORIGINAL_DOCINFO, xmp=ORIGINAL_XMP)
    xmp_before = _xmp_bytes(path)
    updater = MetadataUpdater()
    snapshot = updater.snapshot(str(path))
    json.dumps(snapshot)  # goes into the JSON log

    updater.update_metadata(str(path), MetadataUpdate(title="New", authors="Ada Moreno",
                                                      doi="10.1234/x.1"))
    assert _docinfo(path)["/Title"] == "New"

    updater.restore(str(path), snapshot)
    assert _docinfo(path) == ORIGINAL_DOCINFO
    assert _xmp_bytes(path) == xmp_before


def test_snapshot_of_non_utf8_xmp_survives_the_json_log(tmp_path):
    """XMP may be UTF-16 (or damaged); the log must still be writable."""
    path = make_text_pdf(tmp_path / "p.pdf", ["x"])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        stream = pdf.make_stream(ORIGINAL_XMP.encode("utf-16"))
        stream.Type, stream.Subtype = pikepdf.Name.Metadata, pikepdf.Name.XML
        pdf.Root.Metadata = pdf.make_indirect(stream)
        pdf.save(path, fix_metadata_version=False)  # else pikepdf re-encodes it
    xmp_before = _xmp_bytes(path)
    updater = MetadataUpdater()
    snapshot = json.loads(json.dumps(updater.snapshot(str(path)), ensure_ascii=False)
                          .encode("utf-8"))

    updater.update_metadata(str(path), MetadataUpdate(title="New", authors="A"))
    updater.restore(str(path), snapshot)
    assert _xmp_bytes(path) == xmp_before


def test_restore_removes_xmp_the_original_lacked(tmp_path):
    path = make_text_pdf(tmp_path / "p.pdf", ["x"], docinfo={"/Title": "Old"})
    updater = MetadataUpdater()
    snapshot = updater.snapshot(str(path))
    updater.update_metadata(str(path), MetadataUpdate(title="New", authors="A"))
    assert _xmp_bytes(path) is not None

    updater.restore(str(path), snapshot)
    assert _xmp_bytes(path) is None
    assert _docinfo(path) == {"/Title": "Old"}


def test_restore_keeps_timestamps(tmp_path):
    path = make_text_pdf(tmp_path / "p.pdf", ["x"])
    updater = MetadataUpdater()
    snapshot = updater.snapshot(str(path))
    os.utime(path, (1_500_000_000, 1_500_000_000))
    updater.restore(str(path), snapshot)
    assert os.stat(path).st_mtime == 1_500_000_000


# --- end to end ------------------------------------------------------------------

def test_undo_restores_metadata(tmp_path, capsys):
    path = make_text_pdf(tmp_path / FILENAME, ["x"], docinfo=ORIGINAL_DOCINFO, xmp=ORIGINAL_XMP)
    xmp_before = _xmp_bytes(path)
    log = tmp_path / "run.json"
    assert main([str(path), "--from-filename", "--batch", "--no-rename", "--quiet",
                 "--log", str(log)]) == 0
    assert _docinfo(path)["/Title"] == "Sediment Transport in Braided Rivers"

    assert main(["--undo", str(log)]) == 0
    assert _docinfo(path) == ORIGINAL_DOCINFO
    assert _xmp_bytes(path) == xmp_before
    assert "Restored" in capsys.readouterr().out


def test_undo_renames_back(tmp_path):
    name = "Moreno - 2019 - Sediment Transport in Braided Rivers, a Study.pdf"
    path = make_text_pdf(tmp_path / name, ["x"], docinfo=ORIGINAL_DOCINFO)
    log = tmp_path / "run.json"
    assert main([str(path), "--from-filename", "--batch", "--quiet", "--log", str(log)]) == 0
    entry = json.loads(log.read_text())["results"][0]
    renamed = tmp_path / entry["new_filename"]
    assert renamed.exists() and not path.exists()

    assert main(["--undo", str(log)]) == 0
    assert path.exists() and not renamed.exists()
    assert _docinfo(path) == ORIGINAL_DOCINFO


def test_undo_leaves_a_file_changed_since(tmp_path, capsys):
    path = make_text_pdf(tmp_path / FILENAME, ["x"], docinfo=ORIGINAL_DOCINFO)
    log = tmp_path / "run.json"
    main([str(path), "--from-filename", "--batch", "--no-rename", "--quiet", "--log", str(log)])
    MetadataUpdater().update_metadata(str(path), MetadataUpdate(title="Edited by hand",
                                                                authors="Someone"))

    assert main(["--undo", str(log)]) == 1
    assert _docinfo(path)["/Title"] == "Edited by hand"
    assert "changed since" in capsys.readouterr().err


def test_undo_wont_overwrite_a_file_at_the_old_name(tmp_path, capsys):
    name = "Moreno - 2019 - Sediment Transport in Braided Rivers, a Study.pdf"
    path = make_text_pdf(tmp_path / name, ["x"], docinfo=ORIGINAL_DOCINFO)
    log = tmp_path / "run.json"
    main([str(path), "--from-filename", "--batch", "--quiet", "--log", str(log)])
    renamed = tmp_path / json.loads(log.read_text())["results"][0]["new_filename"]
    path.write_bytes(b"another file now")

    assert main(["--undo", str(log)]) == 1
    assert path.read_bytes() == b"another file now"
    assert _docinfo(renamed)["/Title"] == "Sediment Transport in Braided Rivers, a Study"
    assert "already exists" in capsys.readouterr().err


def test_undo_reports_a_missing_file(tmp_path, capsys):
    path = make_text_pdf(tmp_path / FILENAME, ["x"])
    log = tmp_path / "run.json"
    main([str(path), "--from-filename", "--batch", "--no-rename", "--quiet", "--log", str(log)])
    path.unlink()
    assert main(["--undo", str(log)]) == 1
    assert "not found" in capsys.readouterr().err


def test_undo_ignores_dry_run_and_skipped_entries(tmp_path):
    path = make_text_pdf(tmp_path / FILENAME, ["x"], docinfo=ORIGINAL_DOCINFO)
    log = tmp_path / "run.json"
    main([str(path), "--from-filename", "--batch", "--dry-run", "--quiet", "--log", str(log)])
    assert main(["--undo", str(log)]) == 0
    assert _docinfo(path) == ORIGINAL_DOCINFO


def test_undo_refuses_entries_without_a_snapshot(tmp_path, capsys):
    log = tmp_path / "old.json"
    log.write_text(json.dumps({"session": {}, "results": [{
        "original_path": str(tmp_path / "a.pdf"), "status": "success",
        "new_filename": "b.pdf"}]}))
    assert main(["--undo", str(log)]) == 1
    assert "no record of the previous metadata" in capsys.readouterr().err


def test_undo_with_missing_log(tmp_path, capsys):
    assert main(["--undo", str(tmp_path / "nope.json")]) == 1
    assert "nope.json" in capsys.readouterr().err


def test_paths_required_without_undo(capsys):
    with pytest.raises(SystemExit):
        parse_arguments([])


def test_undo_takes_no_paths(tmp_path, capsys):
    log = tmp_path / "run.json"
    log.write_text(json.dumps({"session": {}, "results": []}))
    assert main(["--undo", str(log), "a.pdf"]) == 1
