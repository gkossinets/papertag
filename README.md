# papertag

Fix the metadata of academic PDFs and give them sensible filenames.

`papertag` finds a paper's DOI or title in the PDF, looks it up on
[Crossref](https://www.crossref.org/), writes the correct title, authors, year,
journal and DOI into the PDF's metadata, and renames the file Zotero-style:

```
393440a0.pdf  ->  Watts and Strogatz - 1998 - Collective dynamics of small-world networks.pdf
```

File timestamps are preserved.

## Install

Requires Python 3.10+.

```sh
uv tool install -e '.[ocr]'      # or: pipx install -e '.[ocr]'
export CROSSREF_EMAIL="you@example.com"   # identifies you to Crossref's polite pool
```

The `ocr` extra (for scanned PDFs) also needs Tesseract and Poppler:
`brew install tesseract poppler`.

## Usage

```sh
papertag paper.pdf                    # interactive: review matches, pick one
papertag ~/Downloads/                 # every PDF in a directory
papertag papers/ --batch --recursive  # auto-accept matches scoring >= 0.80, skip the rest
papertag paper.pdf --no-rename        # update metadata only
papertag "Smith - 2020 - Title.pdf" --from-filename   # metadata from the filename, no Crossref
papertag papers/ --batch --log run.json               # write a JSON log of every action
```

Run `papertag --help` for all options.

### Interactive choices

For each file you see the current metadata and up to five Crossref candidates
with confidence scores. Pick a number to apply it, enter a DOI by hand, use the
filename's metadata, retry, skip, or quit.

### What gets written

| PDF field          | Value                               |
|--------------------|-------------------------------------|
| Title / `dc:title` | Title                               |
| Author / `dc:creator` | Authors, separated by `;`        |
| CreationDate       | January 1 of the publication year   |
| Subject            | `Journal | DOI: 10.xxxx/...`        |
| Keywords / `dc:identifier` | DOI                         |

Filenames: `Author - Year - Title.pdf`, with `Author and Author` for two
authors and `Author et al.` for three or more. Files with missing metadata get
a leading `_`. Name conflicts get ` (1)`, ` (2)`, ...

## Development

```sh
python -m pytest
```

See [PROJECT.md](PROJECT.md) for architecture, decisions and the roadmap.

## License

MIT
