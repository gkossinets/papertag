# papertag — project notes

## Architecture

```
papertag/
  cli.py                  argparse entry point + PDFMetadataManager orchestrator
  core/filename_parser.py hints (author, year, title) from existing filenames
  core/pdf_processor.py   page-1 text extraction (pypdf), OCR fallback, DOI/title heuristics
  core/crossref_client.py Crossref search + DOI lookup, retry/backoff, match scoring
  core/metadata_updater.py write docinfo + XMP (pikepdf), Zotero-style filenames, rename
  ui/interactive.py       prompts: match selection, confirmation, error handling
  utils/logger.py         opt-in JSON session log (--log)
  utils/timestamp_utils.py preserve mtime/atime (+ macOS creation time via SetFile)
tests/                    pytest; run `python -m pytest` (PAPERTAG_NETWORK_TESTS=1 for live Crossref)
  pdf_fixtures.py         builds real one-page text PDFs with pikepdf
```

Per-file flow: filename hints -> extract text/DOI -> DOI lookup or Crossref search
-> pick match (batch: top score >= 0.80; interactive: user) -> write metadata -> rename.

## Decisions

- 2026-11-26: Renamed from `fix-pdf-metadata` / `pdf-metadata-manager` to **papertag**
  (repo, package, command). Packaged with pyproject.toml; install with
  `uv tool install -e '.[ocr]'`. Old `backup/` scripts and wrapper removed (in git history).
- Two-author filenames use `and`, not `&` (sanitizer replaces `&`). Conflicts get ` (1)`.
- Long titles are cut to 97 chars with no ellipsis (sanitization would strip trailing dots).
- XMP `dc:creator` is split on `;` only, so "Last, First" names survive the XMP->docinfo sync.
- 2026-11-26: Exit codes: 0 = no failures (skips are not failures), 1 = any file failed or bad
  arguments, 130 = interrupted.
- 2026-11-26: No fallback PDF writer. If pikepdf can't write, raise `PDFUpdateError` and leave the
  file untouched; rebuilding with another library loses bookmarks/XMP.
- 2026-11-26: Library modules use `logging.getLogger(__name__)` and never print. `cli.configure_logging`
  sends the `papertag` logger to stderr (INFO default, DEBUG with -v, ERROR with -q) and holds
  `pypdf` at ERROR unless -v. UI prompts/progress stay on stdout. No emoji; the confidence stars
  and box-drawing borders stay.
- 2026-11-26: `CrossrefClient.fetch_metadata` returns `CrossrefMatch` (score 1.0, optional `isbn`).

## Roadmap

1. ~~Foundation: rename, pyproject, green tests, remove dead code.~~ (done 2026-11-26)
2. ~~Correctness: multiple input paths, exit codes, `--version`, pypdf extraction, no lossy
   write fallback, `fetch_metadata` -> `CrossrefMatch` (+ ISBN), logging to stderr, no emoji.~~
   (done 2026-11-26)
3. Algorithm. Work in this order (agreed 2026-11-26); each item is testable offline with
   `tests/pdf_fixtures.py`. Run one session per step.
   1. Read DOI/title already embedded in the PDF (XMP `prism:doi`, `pdfx:doi`, `crossmark:DOI`,
      `dc:identifier`, docinfo `/Title`) before text heuristics. Biggest gain for least work:
      many publisher PDFs carry their DOI, which turns a fuzzy search into an exact lookup.
   2. Scoring: max is 0.9 today (journal weight unused) and 0.7 without an author ->
      normalize by available evidence; token-based title similarity; whole-word author match
      ("Li" must not match "Lipton"). This is what makes batch mode useful without a DOI.
   3. Title = largest font on page 1 (pypdf visitor or pdfminer.six). Fixes most wrong searches,
      which today come from guessing the title by line order and capitalization.
   4. Crossref `query.bibliographic` / `query.author`, `select=`
   5. Batch: require margin over runner-up; `--threshold`
   6. Verify a DOI found in page text against the page's title (reference lists contain
      other papers' DOIs)
   7. arXiv IDs, ISBNs
4. UX
   - `--dry-run`; skip already-processed files (`--force` to override)
   - side-by-side current vs new metadata; `--undo LOG`
   - make the Crossref email optional
   - progress display emits cursor-up escape codes (`\x1b[F\x1b[K`) even when stdout is not a
     TTY; only redraw when `sys.stdout.isatty()`
   - batch with an unreadable PDF prints the error on stderr before that file's stdout header when
     piped (stdout is block-buffered); consider flushing stdout before logging errors
