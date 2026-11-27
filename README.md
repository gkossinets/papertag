# papertag

papertag is a command-line tool that updates the metadata of academic PDFs and
gives them Zotero-style filenames.

PDFs downloaded from journals, preprint servers and colleagues often carry
missing or wrong metadata (a title like `manuscript_final_v2.pdf`, no
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
3. **Identification.** If the PDF carries its DOI in its metadata (most
   publisher downloads do), papertag looks it up directly. A DOI printed on
   page 1 is looked up too, but its record is used only if its title appears
   on the page: page 1 can also carry a cited paper's DOI or the DOI of the
   supplementary material, or of the paper's data or code. An arXiv preprint is identified by the ID in its
   margin stamp, or by an arXiv-style filename (`2101.00001v2.pdf`), and its
   record comes from [DataCite](https://datacite.org/), where arXiv registers
   its DOIs; so do DOIs Crossref doesn't have (Zenodo, many repositories). A
   book or chapter with an ISBN on page 1 or in its metadata gets the record of
   that book (or chapter) whose title is the page's. SSRN and NBER working-paper
   numbers on page 1 are looked up as DOIs too. Otherwise it searches Crossref with the title stored in the PDF,
   or one guessed from the page text, plus the authors, and scores up to five
   candidates. A thesis title page ("submitted in partial fulfillment of the
   requirements for the degree of ...") also searches DataCite's dissertations;
   a dissertation record's journal is written as "PhD dissertation, University
   of X" (or "Master's thesis, ..."). If no registry has the thesis,
   interactive mode offers a record read from the title page itself, last, at
   0.00 and with a confirmation step. Metadata written by an earlier papertag run is not trusted, so
   re-running can correct a wrong earlier match.
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

An email address is optional. Crossref puts clients that send one into its
faster "polite pool"; without one, papertag uses the public pool. Set it once:

```sh
export CROSSREF_EMAIL="you@example.com"
```

or pass `--email` on each run.

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
papertag papers/ --batch --dry-run    # show what would change; write nothing
papertag paper.pdf --no-rename        # update metadata only, keep the filename
papertag "Smith - 2020 - Title.pdf" --from-filename   # metadata from the filename, no Crossref
papertag papers/ --batch --log run.json               # write a JSON log of every action
papertag --undo run.json                              # put back what that run changed
```

Paths can be files, directories or glob patterns; quoted patterns such as
`'papers/**/*.pdf'` are expanded by papertag itself. Each file is processed
once, even if it matches more than one argument.

Files that papertag has already tagged are skipped, so re-running over a
folder only handles new files. Pass `--force` to process them again (for
example, to fix a wrong pick). papertag recognizes its own work by the
`DOI: ...` or `ISBN: ...` it writes into the Keywords field, so files tagged
without a DOI or ISBN (`--from-filename`, a thesis title page) are processed
again.

This also skips files tagged by older versions of papertag, including any
wrong picks those versions made in batch mode, so newer matching rules never
reach them on their own. In interactive mode, re-running a file to fix a bad
match just reports it as skipped. To redo a library with the current rules,
preview the changes first, then apply them:

```sh
papertag papers/ --batch --force --dry-run   # show what would change
papertag papers/ --batch --force --log redo.json
```

With `--log`, `papertag --undo redo.json` reverses the redo if it goes wrong.

## Options

| Option | Description |
|--------|-------------|
| `-e`, `--email EMAIL` | Email for Crossref's polite pool, optional (default: `$CROSSREF_EMAIL`) |
| `-b`, `--batch` | Apply the best match without asking if it scores at least the threshold and clearly beats the runner-up; skip the file otherwise |
| `-t`, `--threshold SCORE` | Minimum score for `--batch` to apply a search result (default: 0.80) |
| `-n`, `--dry-run` | Show what would change (metadata and new filename) but write and rename nothing |
| `-f`, `--force` | Process files papertag has already tagged (skipped by default) |
| `-r`, `--recursive` | Include PDFs in subdirectories |
| `--no-rename` | Update metadata but keep the current filename |
| `--from-filename` | Take metadata from an `Author - Year - Title.pdf` name; skip Crossref |
| `--backup` | Keep a copy of each original as `file.pdf.bak` |
| `--no-ocr` | Never OCR, even for scanned PDFs |
| `--ocr-pages N` | Number of pages to OCR (default: 1) |
| `--retries N` | Attempts per Crossref request, with exponential backoff (default: 3) |
| `--log PATH` | Write a JSON log of the session to `PATH` (needed for `--undo`) |
| `--undo LOG` | Restore the metadata and filenames that the run logged in `LOG` changed |
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

- If the PDF's metadata carries a DOI, its Crossref record is applied (score
  1.00). So is the record for a DOI printed on page 1, if that record's title
  appears on the page. Up to three page-1 DOIs are tried, best first: labeled
  ones (`doi: ...`), then `doi.org` links, then bare ones. A DOI split across
  two lines after a hyphen is rejoined, and a supplementary-material suffix
  (`.supp`, `/-/DCSupplemental`) is dropped. The arXiv ID from the page's
  margin stamp comes first; an arXiv ID from the filename comes after the
  page's DOIs, since a published version may keep the arXiv name.
- With no DOI, an ISBN is looked up: of the records Crossref has for that
  book (the book and its chapters), the one whose title matches the page's
  title (similarity >= 0.85) and appears on the page is applied (score 1.00).
  A chapter beats the book when both match.
- Otherwise the best search result is applied only if it scores at least the
  threshold (0.80, or `--threshold`) and leads the next candidate by at least
  0.05. Two near-equal candidates mean papertag can't tell which paper the file
  is, so it skips. A preprint is not counted as a rival of the article, or the
  other way round: they are usually the same work, and papertag has already
  picked the one that fits the PDF (see Limitations).
- Files that cannot be read or written are reported as failed, and processing
  continues.

The score averages the evidence the search had: title similarity (weight 0.5,
by shared words), author surname (0.2, whole words) and year (0.2; half credit
if off by one). Evidence that is missing, such as an unknown year, doesn't
count against a result. To pass the 0.80 cutoff a result needs a near-identical
title plus a matching author or year. A title alone, or an author and year
without a title, is never enough.

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

Before writing, papertag shows the file's current metadata and filename next
to the new ones, with changed rows marked `*`:

```
            Current                                 New
* Title     Microsoft Word - draft3.docx            Sediment Transport in Braided Rivers
* Authors   amoreno                                 Ada Moreno
  Year      2019                                    2019
* Filename  scan0042.pdf                            Moreno - 2019 - Sediment Transport in
                                                    Braided Rivers.pdf
```

Progress, candidates and prompts go to stdout. Errors and warnings go to
stderr as `ERROR: ...` / `WARNING: ...`, so you can redirect them separately:

```sh
papertag papers/ --batch 2> problems.txt
```

`--log PATH` writes a JSON record of the session: the settings used, and for
each file its status (`success`, `dry-run`, `skipped`, `failed`), the matched DOI and
score, the new filename, the reason it was skipped, or the error. For each
file it changed, the log also keeps the file's previous metadata (the whole
document info dictionary and XMP packet), so a log can be a few KB per file.

`papertag --undo run.json` uses that to put files back: it restores the old
metadata and the old filename, last file first, keeping timestamps. A file is
left as is, and reported on stderr, if it is no longer where the run put it,
if its title or authors have changed since the run, or if another file now
has its old name. Undo needs a run made with `--log`; without a log, `--backup`
is the way back.

Exit status:

| Code | Meaning |
|------|---------|
| `0` | No file failed (skipped files are not failures) |
| `1` | At least one file failed, or the arguments were invalid |
| `130` | Interrupted with Ctrl-C |

## Limitations

- The title is the largest text on page 1. When a journal name is set larger
  than the title (some SAGE and Taylor & Francis layouts), the search uses the
  journal name and usually finds nothing to accept. Authors are still guessed
  from the order and capitalization of the lines, which is often wrong; a file
  named `Author - Year - Title.pdf` supplies a better author. A DOI printed on
  the first page gives the most reliable result.
- Without a DOI, batch mode can still rename a file wrongly. The lead it
  requires over the runner-up only helps when two candidates score close
  together. When the paper itself isn't in Crossref, a duplicate record of
  the same work (an abstract, a reprint, a dissertation) often tops the list
  alone and is accepted. In replays of recorded searches with the right
  record removed, that happened about 1 time in 25. Between a preprint and
  the article, it picks the preprint if page 1 carries a preprint server's
  stamp (arXiv, bioRxiv, SSRN, ...), otherwise the article. It does not check
  that the two really are the same paper. Run batches with `--log` and look
  over the renames.
- A DOI printed on page 1 is checked only by whether its record's title
  appears somewhere on the page. A cited paper whose full reference, title
  included, is on page 1 can pass that check and be applied instead. In a
  400-PDF sample this did not happen, but nothing prevents it. A page whose
  text layer is badly damaged can fail the paper's own record; papertag then
  searches, and interactive mode lists the record last at 0.00.
- A record read from a thesis title page is never applied in batch mode; its
  author comes only from a "by" line (or a well-formed filename), so check it
  before confirming.
- Only Crossref is searched, plus DataCite for theses. Papers
  with no Crossref record and no DOI or arXiv ID (many books, most theses and
  working papers) need `--from-filename` or a manual DOI. An ISBN helps only
  when Crossref lists it for the book: an ISBN printed on a chapter's first
  page is not always one Crossref has, and then search decides as before.
- An arXiv PDF gets the arXiv record even when the paper has since been
  published, because that is the version the file is.

Improvements to these are planned; see the roadmap in
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
  core/crossref_client.py   Crossref search, DOI lookup (Crossref, then DataCite), ISBN lookup, retries, scoring
  core/metadata_updater.py  writes metadata, builds filenames, renames
  ui/interactive.py         prompts and progress display
  utils/                    JSON session log, timestamp preservation
```

### Measuring match quality

Two tools measure what batch mode would do, without changing any PDF:

```sh
python -m tests.scoring_benchmark          # recorded Crossref searches, offline
python tools/eval_library.py -r ~/Papers   # your own PDFs (sends queries to Crossref)
```

`tools/eval_library.py` uses the DOI that publishers embed in many PDFs as the
right answer. It hides that DOI, checks the page-1 DOIs as papertag does, runs
papertag's search, and counts right
accepts, wrong accepts and skips for three query sources: what papertag sends,
page metadata only, and the filename only. Crossref responses are cached in
`~/.cache/papertag/eval-crossref`, so a rerun after a code change compares like
with like. `--save FILE` writes per-file results for comparison. Results name
your files, so the tool refuses to save them inside the repository.

It measures only PDFs with a publisher-embedded DOI, mostly recent journal
articles, and most of those also print their DOI on page 1. Files without a DOI
anywhere (books, chapters, older scans, working papers) depend most on title
search and aren't measured. Spot-check some of those by hand: run papertag
interactively on a few and look at the candidates it offers (`q` quits without
changing anything), or run `--batch --log` on copies.

[PROJECT.md](PROJECT.md) has the architecture notes, design decisions and
roadmap.

## License

MIT. See [LICENSE](LICENSE).
