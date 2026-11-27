# papertag — project notes

## Architecture

```
papertag/
  cli.py                  argparse entry point + PDFMetadataManager orchestrator
  core/filename_parser.py hints (author, year, title) from existing filenames
  core/pdf_processor.py   page-1 text extraction (pypdf), OCR fallback, DOI/title heuristics
  core/crossref_client.py Crossref search, DOI lookup (DataCite fallback), ISBN lookup, scoring
  core/metadata_updater.py write docinfo + XMP (pikepdf), Zotero-style filenames, rename
  ui/interactive.py       prompts: match selection, confirmation, error handling
  undo.py                 --undo LOG: restore metadata/filenames from a session log
  utils/logger.py         opt-in JSON session log (--log)
  utils/timestamp_utils.py preserve mtime/atime (+ macOS creation time via SetFile)
tests/                    pytest; run `python -m pytest` (PAPERTAG_NETWORK_TESTS=1 for live Crossref)
  pdf_fixtures.py         builds real one-page text PDFs with pikepdf
```

Per-file flow: skip if already tagged (unless --force) -> filename hints -> extract
text/DOI/arXiv ID/ISBN -> DOI lookup, else ISBN lookup, else Crossref search
-> pick match (batch: `batch_choice`; interactive: user) -> show current vs new
-> `PDFMetadataManager._apply`: write metadata, rename, log (with --dry-run: log only).

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

- 2026-11-26: Batch accepts (step 3.5) go through `crossref_client.batch_choice`: top score
  >= threshold (`--threshold`/`-t`, default `BATCH_THRESHOLD` 0.80, range (0, 1]) and a lead of
  >= `BATCH_MARGIN` 0.05 over the best rival, even a rival below the threshold. A rival is any other
  candidate except an article/preprint counterpart of the pick (`CrossrefMatch.work_type`;
  rank_key already chose between them). That exclusion does not check they're the same work. The
  benchmark replay and `tools/eval_library.py` call the same function. Margin is a constant, not
  a flag: 0.02-0.05 give the same benchmark result, 0.10 costs 7 more right accepts.
- 2026-11-26: Page-1 DOIs (step 3.6). `PDFProcessor._extract_dois` collects every DOI on page 1,
  best first (labeled, then doi.org links, then bare; page order within each), into
  `PDFMetadata.doi_candidates`. Repairs: a DOI broken after a hyphen at a line end is rejoined;
  `.supp` and `/-/DCSupplemental` are stripped; a later copy with a glued-on word or URL path
  (next char a letter or `/`) is dropped. A later *shorter* copy is kept after the earlier one,
  never in its place: it may be a clean copy or a DOI cut at a wrap, and only a lookup can tell.
  `cli.lookup_doi`: an embedded DOI is trusted; otherwise up to 3 page DOIs are fetched and the
  first whose record passes `CrossrefClient.shown_on_page` wins, else search. The check: not a
  supplement record ("Supplementary Materials for ..."), has title words, and >= 60% of them are
  on the page (a 5+ letter word also counts inside the page text with spaces removed, for
  run-together text layers and CJK). Rejected records are appended after search results at score
  0.00, so interactive users can pick one and batch never accepts one. Connection errors stop
  the lookups; 404s move to the next DOI.
- 2026-11-26: arXiv IDs and ISBNs (step 3.7). `CrossrefClient.fetch_metadata` tries DataCite
  (`api.datacite.org/dois/<doi>`) when Crossref 404s, and goes there directly for `10.48550/`
  (arXiv's DataCite prefix; Crossref has none of them). DataCite requests carry no email: it's
  for Crossref's polite pool. Only a 404 falls through; connection errors still raise.
  `CrossrefAPIError.status_code` tells them apart. DataCite `resourceTypeGeneral` maps to Crossref
  types (Preprint -> posted-content); every `10.48550/arXiv.` record is posted-content, since
  some say "Text". arXiv IDs: the margin stamp (ID + version + `[category]`) only, never
  a cited `arXiv:...`; it becomes `10.48550/arXiv.<id>` and goes first in `doi_candidates`
  (the stamp says the PDF is that preprint, so it beats a published DOI on the page; consistent
  with the preprint/article rule). An arXiv filename (`2101.00001v2.pdf`, `arXiv-...`; month
  01-12 checked) sets `FilenameHints.arxiv_id` and no other hint; `lookup_doi` tries it after
  the page DOIs. All are checked with `shown_on_page`. ISBNs: labeled only (`ISBN`, `ISBN-10/13`),
  checksum-valid, normalized to ISBN-13; XMP `prism:isbn`/`pdfx:isbn` first, then page 1 (never
  free-text Keywords). `cli.lookup_isbn` runs when no DOI resolved: Crossref
  `filter=isbn:` + `query.bibliographic=<title>` (20 rows), and `CrossrefClient.isbn_record`
  takes the record whose title (or title: subtitle) has similarity >= 0.85 to the query title
  and is on the page; a chapter beats a book type on a tie. Score 1.0. User's choice: Crossref
  only for ISBNs, no Open Library (a chapter PDF would get the whole book's record).
  `shown_on_page` also rejects data and code records (type dataset/software/component, or a
  title starting "Data from:"/"Data for:"): with DataCite in the loop, a dataset DOI in a
  page-1 data-availability note resolves, and Dryad titles datasets after the paper. Raised by a
  side review; not seen in the 400-PDF sample. Embedded DOIs are still trusted unchecked.
- 2026-11-26: Theses and working papers (step 3.8). User's choices: a thesis no registry has
  gets a record read from its title page, offered in interactive mode only (last, at 0.00,
  no DOI) and confirmed before writing like a typed DOI (raised by a side review: it is
  checked against nothing and may lack an author); journal = "<degree>, <university>" with
  degree "PhD dissertation" / "Master's thesis" ("Doctoral dissertation" for other doctorates,
  "Thesis" when unstated); OpenAlex is a separate step (3.9). `PDFProcessor._thesis_info`
  needs a degree statement ("partial fulfillment", "requirements for the degree", "thesis
  submitted/presented"), not a mention. PhD beats earlier degrees listed on the page; the
  university nearest after "submitted to"/"faculty of"/"graduate school"/"requirements for the
  degree" wins, a university over a college inside it, never an earlier degree's
  ("B.A., X College"); leading "The" dropped. Author only from a "by" line (else a trusted
  filename author); year = copyright year, else the last year on the page. Thesis PDFs also
  search DataCite (`resource-type-id=dissertation`, plain words: the query is Elasticsearch
  syntax), scored by the Crossref scorer on a Crossref-shaped item (`_datacite_item`); a
  DataCite failure leaves Crossref's results. Any `dissertation`-type record gets the journal
  label (`cli.thesis_journal`), using the page's university over the registry's publisher
  (often "<University> Library"). DataCite name-only creators "Family, Given." are flipped.
  Working papers: `ssrn.com/abstract=N` -> `10.2139/ssrn.N`; "Working Paper N" on a page that
  says NBER -> `10.3386/wN`. Both go after the page's DOIs (a published paper may cite its
  working-paper version), checked like page DOIs. SSRN registered DOIs for only some IDs.

- 2026-11-26: UX (step 4). Progress is one plain line per file, never redrawn: the old
  cursor-up redraw erased the last 9 lines of the next file's output (it ran after each file,
  with that file's output in between), not only on non-TTYs. The stderr log handler flushes
  stdout first. The Crossref email is optional (public pool, no mailto in the User-Agent).
  Files tagged by an earlier run are skipped (`PDFProcessor.tagged_by_papertag`, the
  step 3.1 signature check); `--force`/`-f` redoes them. Files tagged without a DOI/ISBN
  (`--from-filename`, title-page records) carry no signature and are processed again; no
  marker field was added. `--from-filename` never skips. One current-vs-new table
  (`InteractiveUI.show_changes`, changed rows `*`, long values wrap) at every decision point:
  confirmation, numbered/batch pick, --from-filename. Interactive mode still lists current
  metadata before the candidates. `--dry-run`/`-n` stops in `_apply`, logs status `dry-run`.
  `--undo LOG` (my calls, not reviewed by the user): the log keeps a `MetadataUpdater.snapshot`
  (whole docinfo, raw XMP; base64 if not UTF-8) and the title/authors as written; undo goes
  last entry first, restores with `fix_metadata_version=False` (pikepdf's default re-serializes
  the XMP), renames back, and refuses a file that moved, changed since, or whose old name is
  taken. Logs stay opt-in, so undo needs `--log`. Non-text docinfo values are not kept.

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
   5. ~~Batch: require margin over runner-up; `--threshold`.~~ (done 2026-11-26) Benchmark: right
      accepts 393 -> 391, wrong 2 -> 0, accepts with the right work absent 22 -> 20 of 497.
      The lost right accepts led a same-titled dissertation record by 0.01. The margin can't help
      the remaining 20 absent accepts: they're a lone duplicate record of the work (abstract,
      reprint), well ahead of everything else. Catching those needs a different signal (record
      type, page count, a DOI on the page). Real-PDF eval (`tools/eval_library.py`) not re-run
      this session; it shares `batch_choice`, so re-run it on the same 37 files to compare.
   6. ~~Verify a DOI found in page text against the page's title.~~ (done 2026-11-26) Survey of
      the 400-PDF sample: 214 have a page-1 DOI, 22 more than one; none of the first DOIs
      belonged to a cited work that resolved. What went wrong was damage: 2 DOIs broken at a
      line end and 2 supplement DOIs (one resolved to a "Supplemental Material for ..." record
      with no authors and would have been written), all now right; 3 unrepairable (garbled,
      unregistered, one footnote citation that 404s) search as before. One right record is
      now rejected (run-together text plus a damaged "fi" ligature); search finds it from the
      filename. Real-PDF eval (37 files): unchanged, 32/7/30 right, 0 wrong (also confirms the
      step 3.5 margin cost nothing there). Known gaps, for later: a cited work whose reference
      on page 1 prints its title passes the check if its DOI is tried first (labeled `doi:`
      references rank ahead of a `doi.org` header link); fix by comparing the record title to
      the page's main title (`PDFMetadata.title`) or scoring all candidates. Not handled either:
      DOIs wrapped without a hyphen (the cut copy is tried, then search), dropped ligatures
      ("Classication"), a Crossref title in another language than the page.
   7. ~~arXiv IDs, ISBNs.~~ (done 2026-11-26) Survey of the 400-PDF sample: 4 distinct arXiv
      PDFs (all stamped; 2 also embed the `10.48550` DOI, which used to 404 in Crossref and fall
      to search), no arXiv filenames, no unstamped arXiv IDs on page 1; 3 ISBN files (2 on
      page 1, 1 in XMP only), all books or chapters without a DOI. Live results, before ->
      after: arXiv 0 -> 4 arXiv records (before: 3 skipped, 1 accepted the published
      proceedings version); ISBN 1 of 3 found (a Cambridge monograph, before skipped). The
      other 2 ISBNs have no Crossref records; search runs as before (one of them already
      found the chapter, whose record lists different ISBNs). Real-PDF eval (37 files):
      unchanged, 32/7/30 right, 0 wrong. Known gaps: a filename arXiv ID may be cut by the
      3-lookup cap on a page with 3+ DOIs; old-style arXiv filenames (`hep-th9901001.pdf`)
      are not read; the record title is written without its Crossref subtitle (as for search).
   8. ~~Dissertations, theses, and working papers.~~ (done 2026-11-26) Survey of the 181 sample
      PDFs without a DOI: thesis title pages carry no ID, handle or URL (0 of 9 by a strict
      test), but all name a university and a year; 1 SSRN ID, no NBER numbers. So the title
      page, not IDs, is the source. Results: 13 PDFs read as theses (1 is a 24-page article
      that mentions a dissertation; it has a DOI, which decides first). Degree and university
      right on all 13 after fixes (by eye); author from a "by" line on 8 (5 empty); year on
      13. Registries: no batch-acceptable record in Crossref or DataCite for any of them, so
      batch still skips them all; interactive now offers the title-page record. The SSRN ID
      has no Crossref DOI (404), so that file searches as before. Known gaps: no "by" line ->
      no author unless the filename is `Author - Year - Title`; a title guessed from line order
      (2 of 13) is often wrong, and the user sees it at confirmation; a record without author
      renames to `_Unknown - Year - Title.pdf`; non-English title pages are not recognized.
   9. Deferred (2026-11-26, user's choice): OpenAlex as a second search source, a possible
      future extension. It indexes repository theses and working papers; would help the ~180
      no-DOI files in the sample, not only theses. Notes from a first look: search currently
      assumes Crossref and needs a provider seam; OpenAlex now meters use (title search
      $1 per 1,000 calls; a free `api_key` gives $1/day), so call it only when Crossref didn't
      settle a file; many of its added records have no DOI, which raises whether batch may
      ever apply one (proposed: interactive only, like the title-page record).
4. ~~UX: `--dry-run`; skip already-processed files (`--force`); side-by-side current vs new
   metadata; `--undo LOG`; optional Crossref email; progress without escape codes; flush stdout
   before diagnostics.~~ (done 2026-11-26) Known gaps: `python -m papertag.cli` logs under
   `__main__`, outside the `papertag` logger, so its messages lack the `ERROR:` prefix (the
   installed command is fine); undo of a file whose restore succeeded but rename-back failed
   leaves the old metadata under the new name.
5. Possible extensions: OpenAlex search (step 3.9 notes); a papertag marker so DOI-less
   tagged files are recognized as processed.
