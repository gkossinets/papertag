#!/usr/bin/env python3
"""Measure papertag's search path on your own PDFs, using their publisher DOIs as answers.

Many publisher PDFs carry their DOI in XMP (prism:doi, pdfx:doi, crossmark:DOI)
or docinfo /DOI. This tool hides that DOI, builds the Crossref search papertag
would send, scores the results as batch mode does, and checks the pick against
the hidden DOI. Nothing is written to the PDFs.

Three query sources are compared:
  papertag  what papertag sends (page metadata, gaps filled from the filename)
  page      page metadata only, as for a freshly downloaded file
  filename  filename hints only (Author - Year - Title.pdf and similar)

Crossref responses are cached (--cache), so re-running after a code change
compares like with like and sends nothing new unless the query changed.
Queries contain your papers' titles and authors; no email is sent.

Usage:
    python tools/eval_library.py -r ~/Papers
    python tools/eval_library.py -r ~/Papers --save ~/papertag-eval/before.json
"""

import argparse
import hashlib
import json
import logging
import sys
import warnings
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from papertag import __version__  # noqa: E402
from papertag.cli import SearchQuery, collect_pdf_files, search_query  # noqa: E402
from papertag.core import FilenameHints, PDFMetadata, PDFProcessor, parse_filename  # noqa: E402
from papertag.core.crossref_client import (BATCH_THRESHOLD, CrossrefClient,  # noqa: E402
                                           batch_choice)

DEFAULT_CACHE = Path.home() / ".cache" / "papertag" / "eval-crossref"
USER_AGENT = f"papertag-eval/{__version__}"
MODES = ("papertag", "page", "filename")


class CachingClient(CrossrefClient):
    """Crossref client that caches raw responses by URL and remembers the last items."""

    def __init__(self, cache_dir: Optional[Path]) -> None:
        super().__init__(email="", retries=6, backoff_factor=2.0)
        # No mailto: the public pool needs no email, and none is sent.
        self.headers = {"User-Agent": USER_AGENT}
        self.min_request_interval = 1.5
        self.cache_dir = cache_dir
        self.last_items: List[Dict[str, Any]] = []
        self.requests_sent = 0

    def _make_request(self, url: str, method: str = "GET") -> Dict[str, Any]:
        cached = self.cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()}.json" \
            if self.cache_dir else None
        if cached and cached.exists():
            data = json.loads(cached.read_text())
        else:
            data = super()._make_request(url, method)
            self.requests_sent += 1
            if cached:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps(data))
        self.last_items = data.get("message", {}).get("items", [])
        return data


def publisher_doi(processor: PDFProcessor, path: str) -> Optional[str]:
    """The DOI a publisher embedded, or None (papertag-written DOIs don't count)."""
    doi, _ = processor._read_embedded_metadata(path, "")
    if doi and doi[1] in processor.PUBLISHER_DOI_SOURCES:
        return doi[0].lower()
    return None


def judge(client: CachingClient, query: SearchQuery, truth: str,
          threshold: float) -> Dict[str, Any]:
    """Run one search and classify what batch mode would do."""
    if not (query.title or query.author or query.year):
        return {"outcome": "no_query", "query": query}
    matches = client.search(title=query.title, author=query.author, year=query.year,
                            author_reliable=query.author_reliable,
                            prefer_preprint=query.preprint)
    found = any(i.get("DOI", "").lower() == truth for i in client.last_items)
    top = matches[0] if matches else None
    accepted = batch_choice(matches, threshold=threshold).match
    if accepted:
        outcome = "right" if accepted.doi.lower() == truth else "wrong"
    else:
        outcome = "skip"
    return {"outcome": outcome, "found": found, "query": query,
            "top": [top.doi, round(top.score, 3), top.title] if top else None}


def evaluate_file(processor: PDFProcessor, client: CachingClient, path: str,
                  threshold: float) -> Optional[Dict[str, Any]]:
    """Evaluate one PDF; None if it has no publisher DOI to check against."""
    truth = publisher_doi(processor, path)
    if not truth:
        return None
    meta = processor.extract_metadata(path)
    hints = parse_filename(Path(path).name)
    page_doi = processor._extract_doi(meta.extracted_text)
    record: Dict[str, Any] = {
        "file": path, "truth": truth,
        "page_doi": "none" if not page_doi else "right" if page_doi.lower() == truth else "other",
        "title": meta.title, "title_source": meta.title_source,
    }
    queries = {
        "papertag": search_query(meta, hints),
        "page": search_query(meta, FilenameHints(author=None, year=None, title=None, confidence=0.0)),
        "filename": search_query(PDFMetadata(), hints),
    }
    for mode in MODES:
        record[mode] = judge(client, queries[mode], truth, threshold)
    return record


def summarize(records: List[Dict[str, Any]], excluded: int, failed: int) -> str:
    """Format the results table."""
    lines = [f"{len(records)} PDFs with a publisher DOI evaluated; "
             f"{excluded} without one skipped; {failed} could not be read.",
             ""]
    if not records:
        return "\n".join(lines)
    page = Counter(r["page_doi"] for r in records)
    lines.append(f"DOI printed on page 1: right {page['right']}, a different one {page['other']}, "
                 f"none {page['none']}  (a page DOI means an exact lookup in normal use)")
    lines.append("")
    lines.append(f"{'query from':<10} {'right':>6} {'wrong':>6} {'skip':>6} {'no query':>9} "
                 f"{'in results':>11}")
    for mode in MODES:
        c = Counter(r[mode]["outcome"] for r in records)
        found = sum(bool(r[mode].get("found")) for r in records)
        lines.append(f"{mode:<10} {c['right']:>6} {c['wrong']:>6} {c['skip']:>6} "
                     f"{c['no_query']:>9} {found:>11}")
    lines.append("")
    lines.append("right/wrong: batch mode would accept the top match (score >= threshold, "
                 "clear lead over the runner-up).")
    lines.append("in results: the right paper was among Crossref's candidates at all.")
    return "\n".join(lines)


def details(records: List[Dict[str, Any]]) -> str:
    """One line per file and mode, for -v."""
    lines = []
    for r in records:
        lines.append(f"{r['file']}  [{r['truth']}]  page title ({r['title_source']}): {r['title']!r}")
        for mode in MODES:
            m = r[mode]
            top = m.get("top")
            lines.append(f"  {mode:<9} {m['outcome']:<8} "
                         + (f"{top[1]:.2f} {top[0]}  {str(top[2])[:60]!r}" if top else ""))
    return "\n".join(lines)


def inside_repo(path: Path) -> bool:
    """True if `path` is in this repository, where personal results must not go."""
    try:
        path.resolve().relative_to(REPO)
        return True
    except ValueError:
        return False


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure papertag's search path on your PDFs, using their publisher DOIs.")
    parser.add_argument("paths", nargs="+", help="PDF files or directories")
    parser.add_argument("-r", "--recursive", action="store_true", help="search directories recursively")
    parser.add_argument("--save", type=Path, metavar="FILE",
                        help="write per-file results as JSON (must be outside the repository)")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, metavar="DIR",
                        help=f"Crossref response cache (default: {DEFAULT_CACHE})")
    parser.add_argument("--no-cache", action="store_true", help="always query Crossref")
    parser.add_argument("--threshold", type=float, default=BATCH_THRESHOLD,
                        help=f"batch accept threshold (default: {BATCH_THRESHOLD})")
    parser.add_argument("-v", "--verbose", action="store_true", help="show each file's outcome")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.save and inside_repo(args.save):
        print(f"error: --save {args.save} is inside the repository; results name your files, "
              f"so save them elsewhere", file=sys.stderr)
        return 1
    if not args.no_cache and inside_repo(args.cache):
        print(f"error: --cache {args.cache} is inside the repository", file=sys.stderr)
        return 1

    files = collect_pdf_files(args.paths, recursive=args.recursive)
    if not files:
        print("error: no PDF files found", file=sys.stderr)
        return 1

    # pypdf/pikepdf recovery warnings would drown the report.
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    logging.getLogger("pikepdf").setLevel(logging.ERROR)
    warnings.filterwarnings("ignore")

    processor = PDFProcessor(use_ocr=False)
    client = CachingClient(None if args.no_cache else args.cache)
    records, excluded, failed = [], 0, 0
    for n, path in enumerate(files, 1):
        try:
            record = evaluate_file(processor, client, path, args.threshold)
        except Exception as e:  # a bad PDF must not end the run; it is counted and shown
            failed += 1
            print(f"error: {path}: {e}", file=sys.stderr)
            continue
        if record is None:
            excluded += 1
        else:
            records.append(record)
        print(f"\r{n}/{len(files)} files, {client.requests_sent} Crossref requests",
              end="", file=sys.stderr)
    print(file=sys.stderr)

    if args.verbose:
        print(details(records))
        print()
    print(summarize(records, excluded, failed))
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(records, ensure_ascii=False, indent=1))
        print(f"\nsaved per-file results to {args.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
