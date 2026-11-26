# papertag — project notes

## Architecture

```
papertag/
  cli.py                  argparse entry point + PDFMetadataManager orchestrator
  core/filename_parser.py hints (author, year, title) from existing filenames
  core/pdf_processor.py   page-1 text extraction (PyPDF2), OCR fallback, DOI/title heuristics
  core/crossref_client.py Crossref search + DOI lookup, retry/backoff, match scoring
  core/metadata_updater.py write docinfo + XMP (pikepdf), Zotero-style filenames, rename
  ui/interactive.py       prompts: match selection, confirmation, error handling
  utils/logger.py         opt-in JSON session log (--log)
  utils/timestamp_utils.py preserve mtime/atime (+ macOS creation time via SetFile)
tests/                    pytest; run `python -m pytest`
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

## Roadmap

1. ~~Foundation: rename, pyproject, green tests, remove dead code.~~ (done 2026-11-26)
2. Correctness
   - accept multiple paths (`nargs='+'`) so shell-expanded `*.pdf` works
   - exit codes: 1 if any file failed, 130 on interrupt; `--version`
   - pikepdf has no `Page.extract_text` (dead path) -> switch text extraction to `pypdf`
   - remove the PyPDF2 write fallback (drops bookmarks/XMP); fail loudly instead
   - `fetch_metadata` should return `CrossrefMatch`; drop `_metadata_dict_to_match` dual format
   - diagnostics to stderr via `logging`; no prints in library modules; drop emoji
3. Algorithm
   - read DOI/title already embedded in the PDF (XMP `prism:doi`, `pdfx:doi`, `crossmark:DOI`,
     `dc:identifier`, docinfo `/Title`) before text heuristics
   - title = largest font on page 1 (pypdf visitor or pdfminer.six)
   - scoring: max is 0.9 today (journal weight unused) and 0.7 without an author ->
     normalize by available evidence; token-based title similarity; whole-word author match
   - Crossref `query.bibliographic` / `query.author`, `select=`
   - batch: require margin over runner-up; `--threshold`
   - verify a DOI found in page text against the page's title
   - arXiv IDs, ISBNs
4. UX
   - `--dry-run`; skip already-processed files (`--force` to override)
   - side-by-side current vs new metadata; `--undo LOG`
   - make the Crossref email optional
