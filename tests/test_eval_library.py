"""tools/eval_library.py, offline: fixture PDFs and a mocked Crossref."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tests.pdf_fixtures import make_text_pdf, xmp_packet

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("eval_library", REPO / "tools" / "eval_library.py")
eval_library = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_library)

PRISM = 'xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/"'
TITLE = "Groundwater Recharge Estimation in Semi-Arid Basins"
BODY = [TITLE, "Ana Moreno and Bernd Fischer",
        "Abstract. We estimate recharge from field data collected over ten years in",
        "three basins, and compare four methods against lysimeter measurements."]


def crossref_item(doi, title=TITLE, family="Moreno"):
    return {"DOI": doi, "title": [title], "author": [{"given": "Ana", "family": family}],
            "issued": {"date-parts": [[2019]]}, "container-title": ["Journal of Hydrology"]}


def respond(items):
    return Mock(status_code=200, headers={}, json=lambda: {"message": {"items": items}})


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    make_text_pdf(lib / "Moreno - 2019 - Groundwater Recharge Estimation in Semi-Arid Basins.pdf",
                  BODY, xmp=xmp_packet(f'{PRISM} prism:doi="10.1016/j.jhydrol.2019.01.001">'))
    make_text_pdf(lib / "no-doi.pdf", BODY)  # nothing to check against
    return lib


def run(args, items, capsys):
    with patch("papertag.core.crossref_client.requests.request",
               return_value=respond(items)) as request, \
         patch("papertag.core.crossref_client.time.sleep"):
        code = eval_library.main(args)
    return code, capsys.readouterr(), request


def test_right_accept_and_exclusion_counted(library, tmp_path, capsys):
    code, out, _ = run([str(library), "--no-cache"],
                       [crossref_item("10.1016/j.jhydrol.2019.01.001")], capsys)
    assert code == 0
    assert "1 PDFs with a publisher DOI evaluated; 1 without one skipped" in out.out
    rows = {line.split()[0]: line.split()[1:] for line in out.out.splitlines()
            if line.split() and line.split()[0] in eval_library.MODES}
    assert rows["papertag"][:3] == ["1", "0", "0"]    # right, wrong, skip


def test_wrong_accept_detected(library, capsys):
    code, out, _ = run([str(library), "--no-cache"],
                       [crossref_item("10.9999/other.paper")], capsys)
    rows = {line.split()[0]: line.split()[1:] for line in out.out.splitlines()
            if line.split() and line.split()[0] in eval_library.MODES}
    assert rows["papertag"][:2] == ["0", "1"]


def test_save_writes_json_outside_repo(library, tmp_path, capsys):
    target = tmp_path / "results" / "before.json"
    run([str(library), "--no-cache", "--save", str(target)],
        [crossref_item("10.1016/j.jhydrol.2019.01.001")], capsys)
    records = json.loads(target.read_text())
    assert records[0]["truth"] == "10.1016/j.jhydrol.2019.01.001"
    assert records[0]["papertag"]["outcome"] == "right"


@pytest.mark.parametrize("flag", ["--save", "--cache"])
def test_refuses_to_write_inside_repo(library, capsys, flag):
    target = REPO / "tests" / "data" / "eval-should-not-exist"
    try:
        code, out, request = run([str(library), flag, str(target)], [], capsys)
        assert code == 1
        assert "inside the repository" in out.err
        request.assert_not_called()
    finally:
        # If the guard ever breaks, don't leave results in the repo.
        if target.is_dir():
            import shutil
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def test_cache_avoids_repeat_requests(library, tmp_path, capsys):
    cache = tmp_path / "cache"
    items = [crossref_item("10.1016/j.jhydrol.2019.01.001")]
    _, _, first = run([str(library), "--cache", str(cache)], items, capsys)
    _, _, second = run([str(library), "--cache", str(cache)], items, capsys)
    assert first.call_count > 0
    second.assert_not_called()


def test_pdf_files_are_not_modified(library, capsys):
    before = {p: p.read_bytes() for p in library.iterdir()}
    run([str(library), "--no-cache"], [crossref_item("10.1016/j.jhydrol.2019.01.001")], capsys)
    assert {p: p.read_bytes() for p in library.iterdir()} == before
