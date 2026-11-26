# papertag

papertag is a command-line tool that updates the metadata of academic PDFs and
gives them Zotero-style filenames.

PDFs downloaded from journals, preprint servers and colleagues often carry
missing or wrong metadata (a title like `Microsoft Word - draft3.docx`, no
authors) and meaningless names like `393440a0.pdf`. papertag reads the first
page, identifies the paper through [Crossref](https://www.crossref.org/),
writes the correct title, authors, year, journal and DOI into the file, and
renames it:

| Before | After |
|--------|-------|
| `393440a0.pdf` | `Watts and Strogatz - 1998 - Collective dynamics of small-world networks.pdf` |

Files keep their original modification and creation dates, so they stay sorted
where you left them.

## Contents

- [How it works](#how-it-works)
- [Install](#install)
- [Usage](#usage)
- [Options](#options)
- [Interactive mode](#interactive-mode)
- [Batch mode](#batch-mode)
- [Metadata from filenames](#metadata-from-filenames)
- [What gets written](#what-gets-written)
- [Output, logs and exit status](#output-logs-and-exit-status)
- [Limitations](#limitations)
- [Development](#development)

## How it works

For each PDF:

1. **Filename hints.** If the current name looks like `Author - Year - Title`,
   `Author (Year) Title`, `Author_Year_Title` or similar, the author, year and
   title are used as search hints.
2. **Text extraction.** The text of page 1 is extracted. If it has almost no
   text (a scan), it is OCR'd, when OCR is installed.
3. **Identification.** If a DOI appears on the page, papertag looks it up
   directly. Otherwise it guesses the title and authors from the page text and
   searches Crossref, scoring up to five candidates.
4. **Selection.** You pick a candidate (interactive mode), or the top one is
   accepted automatically if it scores high enough (batch mode).
5. **Writing.** The PDF's document info and XMP metadata are replaced with the
   Crossref record, and the file is renamed.

## Install

Requires Python 3.10 or newer.

```sh
git clone https://github.com/gkossinets/papertag.git
cd papertag
uv tool install -e '.[ocr]'      # or: pipx install -e '.[ocr]'
```

Crossref asks API clients to identify themselves with an email address, which
gets them into its faster "polite pool". Set it once:

```sh
export CROSSREF_EMAIL="you@example.com"
```

or pass `--email` on each run. It is not needed for `--from-filename`.

**OCR (optional).** Reading scanned PDFs needs the `ocr` extra (installed
above) plus Tesseract and Poppler:

```sh
brew install tesseract poppler       # macOS
sudo apt install tesseract-ocr poppler-utils   # Debian/Ubuntu
```

Without them papertag still works on PDFs that have a text layer, and warns
that OCR is unavailable.

**Creation dates on macOS.** Preserving a file's creation date uses `SetFile`
from the Xcode Command Line Tools (`xcode-select --install`). Without it,
modification and access times are still preserved.

## Usage

```sh
papertag paper.pdf                    # interactive: review matches, pick one
papertag ~/Downloads/                 # every PDF in a directory
papertag papers/ --recursive          # ...and its subdirectories
papertag *.pdf a/ b/ --batch          # any mix of files, directories and globs
papertag papers/ --batch --backup     # unattended; keep a .bak of each original
papertag paper.pdf --no-rename        # update metadata only, keep the filename
papertag "Smith - 2020 - Title.pdf" --from-filename   # metadata from the filename, no Crossref
papertag papers/ --batch --log run.json               # write a JSON log of every action
```

Paths can be files, directories or glob patterns; quoted patterns such as
`'papers/**/*.pdf'` are expanded by papertag itself. Each file is processed
once, even if it matches more than one argument.

## Options

| Option | Description |
|--------|-------------|
| `-e`, `--email EMAIL` | Email for Crossref (default: `$CROSSREF_EMAIL`) |
| `-b`, `--batch` | Apply matches scoring 0.80 or more without asking; skip the rest |
| `-r`, `--recursive` | Include PDFs in subdirectories |
| `--no-rename` | Update metadata but keep the current filename |
| `--from-filename` | Take metadata from an `Author - Year - Title.pdf` name; skip Crossref |
| `--backup` | Keep a copy of each original as `file.pdf.bak` |
| `--no-ocr` | Never OCR, even for scanned PDFs |
| `--ocr-pages N` | Number of pages to OCR (default: 1) |
| `--retries N` | Attempts per Crossref request, with exponential backoff (default: 3) |
| `--log PATH` | Write a JSON log of the session to `PATH` |
| `-v`, `--verbose` | Show debug detail: filename hints, extracted DOI/title/authors, PDF parser warnings, tracebacks |
| `-q`, `--quiet` | Show only errors and the final summary |
| `-V`, `--version` | Print the version and exit |
| `-h`, `--help` | Print help and exit |

## Interactive mode

Without `--batch`, papertag shows each file's current metadata and the
Crossref candidates, best first:

```
Found 3 potential matches for: 393440a0.pdf

1. ★★★ HIGH CONFIDENCE (0.90)
   Title: Collective dynamics of 'small-world' networks
   Authors: Duncan J. Watts; Steven H. Strogatz
   Year: 1998
   Journal: Nature
   DOI: 10.1038/30918

2. ★ LOW CONFIDENCE (0.41)
   ...

Choose: [1-3] Select match | [s]kip | [r]etry | [m]anual DOI | [f]rom filename | [q]uit
>
```

| Key | Action |
|-----|--------|
| `1`–`5` | Apply that candidate immediately |
| `m` | Type a DOI yourself; the record is fetched and shown for confirmation |
| `f` | Use the author, year and title parsed from the current filename (offered only when the filename has them) |
| `r` | Search again (useful after a network error) |
| `s` | Skip this file, leaving it untouched |
| `q` | Stop; the summary is still printed |

Confidence is HIGH at 0.80 and above, MEDIUM from 0.65, LOW below that. If
Crossref finds nothing, you can still enter a DOI or use the filename.

## Batch mode

`--batch` never prompts. For each file:

- If a DOI is found on the first page, its Crossref record is applied (score 1.00).
- Otherwise the best search result is applied only if it scores at least 0.80,
  and the file is skipped if not.
- Files that cannot be read or written are reported as failed, and processing
  continues.

The score is a weighted sum: title similarity up to 0.5, matching year 0.2 (0.1
if off by one), matching author surname 0.2. A search result therefore needs a
close title plus a matching year and author to pass the 0.80 cutoff.

Run a batch with `--log` to review afterwards which files were skipped and why.

## Metadata from filenames

If your files are already named `Author - Year - Title.pdf`, as Zotero names
attachments, `--from-filename` writes that author, year and title into the PDF
without contacting Crossref. It is a quick way to fix the embedded metadata of
a library that is already well named. Files whose names don't follow the
pattern are skipped.

## What gets written

papertag replaces the document info dictionary, keeping only `Creator` and
`Producer`, and updates the matching XMP fields.

| PDF field | XMP field | Value |
|-----------|-----------|-------|
| Title | `dc:title` | Title |
| Author | `dc:creator` | Authors, separated by `;` |
| CreationDate | `xmp:CreateDate` | January 1 of the publication year |
| Subject | `dc:description` | `Journal \| DOI: 10.xxxx/...` |
| Keywords | `pdf:Keywords` | `DOI: 10.xxxx/...` |
| | `dc:identifier` | DOI (or ISBN for books without one) |

XMP properties that record where the file was downloaded from (URLs, "where
from") are removed.

If a write fails, papertag reports the error and leaves the original file
unchanged. Files are written to a temporary copy first and swapped into place
only on success.

**Filenames** follow Zotero's default `Author - Year - Title.pdf`:

| Authors | Filename |
|---------|----------|
| one | `Watts - 1998 - Title.pdf` |
| two | `Watts and Strogatz - 1998 - Title.pdf` |
| three or more | `Watts et al. - 1998 - Title.pdf` |

Quotes, commas, semicolons and parentheses are removed, and titles longer than
100 characters are cut to 97. If the author, year or title is missing, the
name starts with `_` (for example `_Unknown - 1998 - Title.pdf`) so incomplete
files sort together. If the name is already taken, ` (1)`, ` (2)`, ... is
appended. Nothing is overwritten.

## Output, logs and exit status

Progress, candidates and prompts go to stdout. Errors and warnings go to
stderr as `ERROR: ...` / `WARNING: ...`, so you can redirect them separately:

```sh
papertag papers/ --batch 2> problems.txt
```

`--log PATH` writes a JSON record of the session: the settings used, and for
each file its status (`success`, `skipped`, `failed`), the matched DOI and
score, the new filename, the reason it was skipped, or the error.

Exit status:

| Code | Meaning |
|------|---------|
| `0` | No file failed (skipped files are not failures) |
| `1` | At least one file failed, or the arguments were invalid |
| `130` | Interrupted with Ctrl-C |

## Limitations

- The title and authors are guessed from the order and capitalization of the
  lines on page 1, which works poorly for some layouts. A DOI printed on the
  first page gives the most reliable result.
- Without a DOI, scores top out at 0.90, so batch mode skips many papers
  that interactive mode would identify easily.
- Only Crossref is searched. Papers with no Crossref record (most arXiv-only
  preprints, many books, theses) need `--from-filename` or a manual DOI.

Improvements to all three are planned; see the roadmap in
[PROJECT.md](PROJECT.md).

## Development

```sh
uv pip install -e '.[ocr,dev]'      # or: pip install -e '.[ocr,dev]'
python -m pytest
PAPERTAG_NETWORK_TESTS=1 python -m pytest   # also hit the live Crossref API
```

The code is organized as:

```
papertag/
  cli.py                    command-line entry point and per-file workflow
  core/filename_parser.py   search hints from existing filenames
  core/pdf_processor.py     page-1 text, OCR fallback, DOI and title detection
  core/crossref_client.py   Crossref search and DOI lookup, retries, scoring
  core/metadata_updater.py  writes metadata, builds filenames, renames
  ui/interactive.py         prompts and progress display
  utils/                    JSON session log, timestamp preservation
```

[PROJECT.md](PROJECT.md) has the architecture notes, design decisions and
roadmap.

## License

MIT. See [LICENSE](LICENSE).
