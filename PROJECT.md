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
- 2026-11-26: Embedded DOI/title (step 3.1) override page-text heuristics. DOI keys are an
  allowlist, best first: `prism:doi` (any PRISM version), `pdfx:doi`, `crossmark:DOI`,
  `dc:identifier`, docinfo `/DOI`, then a `doi:`-labeled DOI in `/Subject` or `/Keywords`. Never scan
  all fields: Wiley's `pdfx:WPS-JOURNALDOI` is the journal's DOI (`10.1111/(ISSN)...`), so it's rejected.
  `PDFMetadata.doi_source` / `title_source` record where each value came from.
- 2026-11-26: Re-runs ignore papertag's own earlier output, so a wrong batch pick gets fixed, not
  confirmed. Signature: `/Keywords` is exactly `DOI: x`/`ISBN: x`, or `/Subject` ends in
  `DOI: x`/`ISBN: x` (alone or after ` | `). In such files only publisher keys
  (prism/pdfx/crossmark/`/DOI`) count, and the title comes from page text. Reuse this check for the
  step-4 "skip already-processed files" item.
- 2026-11-26: An embedded title must pass a junk filter (authoring-tool and template titles) and,
  when page 1 has >= 100 chars of text, have >= 60% of its words (3+ letters) on the page. On a
  ~400-PDF sample, 77 of 84 clean titles in untouched files passed; the rejects were mostly not papers.
- 2026-11-26: The `pikepdf` logger is treated like `pypdf`: ERROR unless -v (it logs a traceback when
  recovering from malformed XMP).
- Test fixtures use invented, neutral examples, never titles/DOIs from a real library.
- 2026-11-26: Scoring (step 3.2) is a weighted average over the evidence the query has (title 0.5,
  author 0.2, year 0.2; year off by one = half). Caps keep batch (>= 0.80) from accepting on weak
  evidence: title only <= 0.75, no title <= 0.5, title similarity < 0.85 <= 0.75.
- 2026-11-26: Title similarity is word-based Dice after stripping markup, accents and stopwords;
  underscores split words. A damaged word (one edit, same first two letters, 5+ letters) counts 0.9
  of an exact one. Anything looser equated opposites (supervised/unsupervised, linear/nonlinear).
  A 4+ word query matching the start of a longer title (first line of a wrapped title) scores 0.9.
- 2026-11-26: Author match is whole words on family names, letters only (drops affiliation markers);
  particles are ignored, and one part of a hyphenated name is enough.
- 2026-11-26: `tests/data/crossref_benchmark.json.gz`: 395 recorded Crossref searches from random
  public records (sensitive topics filtered out, including candidates) with known answers, plus
  derived query shapes. `python -m tests.scoring_benchmark` prints batch outcomes;
  `tests/test_scoring_benchmark.py` guards them. Re-record with `tools/record_crossref_benchmark.py`
  when the query changes (step 3.4).
- 2026-11-26: Crossref 429 responses are retried (Retry-After, capped at 60 s).

## Roadmap

1. ~~Foundation: rename, pyproject, green tests, remove dead code.~~ (done 2026-11-26)
2. ~~Correctness: multiple input paths, exit codes, `--version`, pypdf extraction, no lossy
   write fallback, `fetch_metadata` -> `CrossrefMatch` (+ ISBN), logging to stderr, no emoji.~~
   (done 2026-11-26)
3. Algorithm. Work in this order (agreed 2026-11-26); each item is testable offline with
   `tests/pdf_fixtures.py`. Run one session per step.
   1. ~~Read DOI/title already embedded in the PDF before text heuristics.~~ (done 2026-11-26)
      Known gaps, judged rare: `dc:title` rdf:Alt returns the first language, not `x-default`;
      raw UTF-8 docinfo `/Title` without BOM decodes as mojibake; a bare journal name as title
      passes the filter; a labeled SICI DOI in `/Subject` is cut at `<`.
   2. ~~Scoring: normalize by available evidence; token-based title similarity; whole-word
      author match.~~ (done 2026-11-26) Benchmark batch accepts 222 -> 389 of 553 queries.
      Known gaps, for step 3.5: duplicate records of one work (preprint/article, abstract/paper)
      tie, and Crossref's order picks; same-author titles differing in one word of a long title
      ("Biliary" vs "Vascular"); "Part I" vs "Part II" when the right part is missing; an erratum
      or a longer title starting with the query, when the right paper is missing.
   3. Title = largest font on page 1 (pypdf visitor or pdfminer.six). Fixes most wrong searches,
      which today come from guessing the title by line order and capitalization.
      Baseline (2026-11-26, 37 real publisher PDFs, DOI hidden, search path only): the page-text
      title is usually a header ("ORIGINAL ARTICLE", journal name, "ARTICLE INFO"); batch accepts
      2/37 right, 0 wrong, and Crossref's top 10 lacks the right paper for 27/37. Filename hints
      alone (Zotero-style names): 30/36 right, 0 wrong. Also fix the precedence in cli.py: a
      high-confidence filename title should beat a page-text guess (`title_source == "text"`);
      today `pdf_metadata.title or filename_hints.title` discards it. (32/37 of these PDFs print
      their DOI on page 1, so in practice they are found by exact lookup.)
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
