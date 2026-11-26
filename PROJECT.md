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
  `tests/test_scoring_benchmark.py` guards them. When the query changes, re-issue the same queries
  with `tools/record_crossref_benchmark.py --requery` (a plain run samples new works, so results
  would not compare).
- 2026-11-26: Crossref 429 responses are retried (Retry-After, capped at 60 s).
- 2026-11-26: `tools/eval_library.py` evaluates on the user's own PDFs (publisher-embedded DOI as
  the answer, hidden). The query comes from `cli.search_query`, shared with the CLI. It never writes
  PDFs and refuses `--save`/`--cache` paths inside the repo: results name personal files.
- 2026-11-26: Title = largest type on page 1 (step 3.3), via pypdf's text visitor (no new
  dependency). Chunks are grouped into blocks of one size in drawing order; position is not used,
  because pypdf reports wrong heights for lines after a line break in some publishers' PDFs.
  Candidates are larger than the body size (most letters), have >= 3 words and <= 300 chars, and
  pass the title blacklist; among the largest and those within 5% of it, the longest wins (Elsevier
  sets the journal name ~4% above the title). Skipped: rotated text (arXiv margin stamp), sizes
  < 2 pt (pypdf reports 1.0 after some mid-line font switches), and short non-letter chunks
  (footnote markers). JSTOR cover pages: cut at "Author(s):". `title_source == "font size"`.
  Precedence: embedded title > font size > line-order guess, except that a font title which
  extends the embedded one (publisher title cut at the colon) wins.
- 2026-11-26: `cli.search_query`: a filename with confidence >= 0.85 (`Author - Year - Title`,
  `Author (Year) Title`) supplies the title when the page title is the line-order guess, and the
  author always: on the held-out files the page-text author held the right surname in 43/283.
- 2026-11-26: Title blacklist publisher names match whole words (bare `SAGE` rejected "Usage").
- 2026-11-26: Crossref search (step 3.4) sends `query.bibliographic=<title> <year>` and
  `query.author=<first author's surname>`, with `select=` limited to the fields scoring and display
  read, built by `CrossrefClient.search_url`. `query.author` nearly filters: a junk surname from the
  page-text guess ("University", "of") cut the right paper from the results for 21 of 37 real PDFs.
  So `cli.SearchQuery.author_reliable` is False for the page-text author, and its surname goes into
  `query.bibliographic` instead. Filename authors are reliable. `first_author_surname` handles
  "X et al.", "X and Y", "Last, First", marked author lines and Vancouver "Moreno A" (the old code
  sent the last word: "al.", or the second author).
- 2026-11-26: On equal scores between a published record and a preprint (`type` posted-content)
  of the same work, the one matching the PDF wins (user's choice): the preprint when page 1 has a
  preprint server's own stamp (`PDFMetadata.preprint`), else the article (`CrossrefClient.rank_key`,
  also used by the benchmark replay). Crossref's order used to decide; the fielded query ranked
  preprints first more often, and batch mode accepted one for an article PDF. Stamps, not
  mentions: arXiv's margin stamp with version, bio/medRxiv headers, a line starting "Preprint",
  "preprint ... not peer reviewed", the SSRN footer, PsyArXiv/OSF and Research Square DOIs. Not
  "arXiv preprint arXiv:..." (a citation) or osf.io (data links). Flags 6 of the 400 sampled PDFs.

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
   3. ~~Title = largest font on page 1.~~ (done 2026-11-26) Real-PDF results, DOI hidden,
      search path only (`tools/eval_library.py`, 37 publisher PDFs): papertag batch accepts
      2 -> 31 right, 0 wrong (filename hints alone: 30). Page only, as for a fresh download with
      a meaningless filename: 2 -> 7 right, 0 wrong; the right paper is in Crossref's results for
      34/37, and the page-text author guess is what keeps most of them under 0.80.
      Those 37 files were also used to write the rules, so their numbers are optimistic. Held-out
      check: 283 other files from the same 400-PDF sample, title extracted vs the Zotero filename
      title (similarity >= 0.85): line-order guess 61, font size 192 (198 with the guess as
      fallback). Further rules were tuned on half of them only; the other half went 92 -> 99 of
      141 from those rules. Filename titles are cut at 97 chars, so "match" undercounts.
      Known gaps: a journal name set larger than the title (SAGE, Taylor & Francis; ~6% of
      held-out files) wins; a same-size section label before the title is joined to it
      ("REVIEW—HYDROLOGY Groundwater ...", invented); letter-spaced type extracts as "N a t u re";
      a title split across two sizes (subtitle set smaller or larger) gives one part.
      Next: authors. The page-text author is a line-order guess anchored on the old title guess;
      the block after the font title (smaller type) is the likely author line.
   4. ~~Crossref `query.bibliographic` / `query.author`, `select=`.~~ (done 2026-11-26) Benchmark,
      same works re-queried: right work among candidates 485 -> 502 of 553, batch right accepts
      389 -> 393, wrong 2 -> 2 (same case), accepts with the right work absent 20/485 -> 22/497.
      Real PDFs (37, DOI hidden): papertag 31 -> 32 right, page only 7 -> 7, filename 30; 0 wrong
      in every row. Compared in an A/B run first: everything in `query.bibliographic` did no
      better than the plain query; the full author line in `query.author` did slightly better
      on the benchmark's clean author lines but was not adopted, since real page author lines
      are mostly not names.
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
