"""
PDF Processor Module

Extracts text, metadata, and DOIs from PDF files with OCR fallback for scanned documents.
"""

import html
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import pikepdf
from pypdf import PdfReader

# OCR libraries (optional)
try:
    import pytesseract
    from pdf2image import convert_from_path
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

log = logging.getLogger(__name__)


# Custom exceptions
class PDFProcessingError(Exception):
    """Base exception for PDF processing errors."""
    pass


class PDFNotFoundError(PDFProcessingError):
    """Raised when PDF file doesn't exist."""
    pass


class PDFReadError(PDFProcessingError):
    """Raised when PDF cannot be read."""
    pass


class OCRNotAvailableError(PDFProcessingError):
    """Raised when OCR is needed but not available."""
    pass


@dataclass
class PDFMetadata:
    """Metadata extracted from a PDF file."""
    title: Optional[str] = None
    authors: Optional[str] = None
    journal: Optional[str] = None
    year: Optional[str] = None
    doi: Optional[str] = None
    extracted_text: str = ""
    used_ocr: bool = False
    # Where doi/title came from, e.g. "xmp prism:doi", "docinfo /Title", "text".
    # A DOI read from page text may belong to a cited work; an embedded one
    # was put there by whoever produced the file.
    doi_source: Optional[str] = None
    title_source: Optional[str] = None
    # Every DOI on page 1, repaired and best first. The first may be a cited
    # work's or the supplement's, so each is checked against the page.
    doi_candidates: List[str] = field(default_factory=list)
    # Page 1 carries a preprint server's stamp; on a score tie the preprint
    # record then beats the published article.
    preprint: bool = False
    # arXiv ID (no version) from page 1's margin stamp: the PDF is that preprint.
    arxiv_id: Optional[str] = None
    # ISBN-13s from XMP (prism:isbn, pdfx:isbn) and page 1, in that order. A
    # chapter PDF carries its book's ISBN, so these name a book, not the file.
    isbn_candidates: List[str] = field(default_factory=list)


ARXIV_DOI_PREFIX = "10.48550/arXiv."


def arxiv_doi(arxiv_id: str) -> str:
    """The DataCite DOI arXiv registered for `arxiv_id` (no version)."""
    return f"{ARXIV_DOI_PREFIX}{arxiv_id}"


def isbn13(text: str) -> Optional[str]:
    """Normalize an ISBN-10 or ISBN-13 to 13 digits, or None if invalid.

    Args:
        text: The ISBN, with or without hyphens and spaces.

    Returns:
        The ISBN-13, or None when the length or check digit is wrong.
    """
    digits = re.sub(r'[-\s]', '', text).upper()
    if re.fullmatch(r'\d{9}[\dX]', digits):
        total = sum((10 - i) * (10 if c == 'X' else int(c)) for i, c in enumerate(digits))
        if total % 11:
            return None
        digits = '978' + digits[:9]
        check = (10 - sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(digits)) % 10) % 10
        return digits + str(check)
    if re.fullmatch(r'97[89]\d{10}', digits):
        if sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(digits)) % 10:
            return None
        return digits
    return None


@dataclass
class TextChunk:
    """A piece of page-1 text as pypdf draws it.

    Attributes:
        text: The text, including any whitespace or newline pypdf emitted.
        size: Rendered font size in points; None for whitespace, whose size
            pypdf does not report reliably.
        rotated: True for text not set left to right (e.g. arXiv's margin stamp).
    """
    text: str
    size: Optional[float]
    rotated: bool = False


@dataclass
class _TextBlock:
    """Consecutive chunks of one font size: a title, a heading, an author line."""
    size: float
    parts: List[str]

    @property
    def text(self) -> str:
        joined = "".join(self.parts)
        # A line ending in a hyphen continues the word on the next line.
        joined = re.sub(r'-\s*\n\s*', '-', joined)
        # JSTOR cover pages set "Author(s): ... Source: ..." in the title's type.
        joined = re.split(r'\s+Author\(s\):', joined)[0]
        return " ".join(joined.split())


class PDFProcessor:
    """Extract text and metadata from PDF files."""

    # DOI detection patterns
    DOI_PATTERNS = [
        r'(?:doi|DOI):?\s*(10\.\d{4,9}/[^\s"\'<>]+)',
        r'https?://doi\.org/(10\.\d{4,9}/[^\s"\'<>]+)',
        r'https?://(doi\.org/10\.\d{4,9}/[^\s"\'<>]+)',
        # Zero-width boundaries, so DOIs one space apart are all found.
        r'(?<![\w.])(10\.\d{4,9}/[^\s"\'<>]+)(?![\w.])',
        r'doi\.org/(10\.\d{4,9}/[^\s"\'<>]+)'
    ]

    # Text extraction breaks a long DOI after a hyphen ("annurev-x-012119-\n045404").
    DOI_LINE_BREAK = re.compile(r'(10\.\d{4,9}/\S*-)[ \t]*\n\s*(?=\w)')
    # The supplementary material's DOI (APA, PNAS): its record is titled
    # "Supplemental Material for ..." and has no authors.
    DOI_SUPPLEMENT_SUFFIX = re.compile(r'(?:\.supp|/-/dcsupplemental)$', re.IGNORECASE)
    # What follows a DOI's copy when a footer glues on the next word
    # ("...1234the") or a URL continues ("/9002026"). A digit after the clean
    # copy is not damage: 10.1/x.1 and 10.1/x.12 are different works.
    DOI_DAMAGE = re.compile(r'[a-zA-Z/]')

    # Blacklist patterns for filtering out non-title lines
    TITLE_BLACKLIST_PATTERNS = [
        r'downloaded from',
        r'all rights reserved',
        r'copyright',
        r'reproduced with permission',
        r'used with permission',
        r'©',
        r'page \d+ of \d+',
        r'\d+\s*?\|\s*?page',
        r'http',
        r'www\.',
        r'@',
        r'volume \d+',
        r'issue \d+',
        r'doi:',
        r'isbn',
        r'issn',
        r'\d{4} by',
        # Whole words: with IGNORECASE, a bare "SAGE" rejected titles with "usage".
        r'\b(?:Elsevier|Springer|Wiley|SAGE|IEEE|Oxford University Press|Cambridge University Press)\b'
    ]

    # Embedded metadata. Key names come from a survey of ~400 real PDFs
    # (publisher downloads, arXiv, and files papertag had already processed).
    DC_NS = "http://purl.org/dc/elements/1.1/"
    # PRISM appears as basic/2.0/, 2.1/ and 3.0/; match any version.
    PRISM_NS_PREFIX = "http://prismstandard.org/namespaces/basic/"
    XMP_DOI_KEYS = [
        ("{http://ns.adobe.com/pdfx/1.3/}doi", "xmp pdfx:doi"),
        ("{http://crossref.org/crossmark/1.0/}DOI", "xmp crossmark:DOI"),
        (f"{{{DC_NS}}}identifier", "xmp dc:identifier"),
    ]
    # /Subject and /Keywords are free text: papertag writes "Journal | DOI: ...",
    # Elsevier writes a citation ending in "doi:...".
    DOCINFO_DOI_KEYS = ["/DOI", "/doi", "/Subject", "/Keywords"]
    LABELED_DOI_FIELDS = {"docinfo /Subject", "docinfo /Keywords"}
    # Fields papertag never writes, so they still count after it has run.
    PUBLISHER_DOI_SOURCES = {"xmp prism:doi", "xmp pdfx:doi", "xmp crossmark:DOI",
                             "docinfo /DOI", "docinfo /doi"}

    JUNK_TITLE_PATTERNS = [
        r'^(?:title|article|document|manuscript|paper|layout|slide)\b[\s\d_-]*$',
        r'^untitled\b',
        r'^microsoft \w+\b',
        r'^powerpoint presentation\b',
        r'^copy of\b',
        r"^(?:author'?s?\s+)?accepted manuscript\b",
        r'^manuscript id\b',
        r'^running head\b',
        r'^electronic copy available\b',
        r'^(?:paper|article) title\b',
        r'^title of the (?:paper|article)\b',
        r'^preparation of papers for\b',
        r'^instructions for (?:authors|contributors)\b',
        r'\btemplate\b',
        r'\.(?:pdf|docx?|rtf|odt|tex|dvi|ps|eps|indd|qxd|pages)$',
        r'\\',                        # Windows path
        r'^pii:',                     # Elsevier production ID
        r'^arxiv:',
        r'10\.\d{4,9}/',              # a DOI, not a title
    ]
    # Share of an embedded title's words that must appear in the page text.
    TITLE_ON_PAGE_MIN_SHARE = 0.6

    # Title by font size. Sizes within this many points are the same type.
    FONT_SIZE_TOLERANCE = 0.2
    # Elsevier sets the journal name ~4% larger than the title; sizes this
    # close compete on length, and the longer block (the title) wins.
    FONT_SIZE_NEAR_RATIO = 0.95
    TITLE_MIN_WORDS = 3          # shorter: a label ("FIELD NOTES"), a drop cap
    # pypdf reports size 1.0 for some text after a font switch mid-line; no
    # real text is this small, so such chunks are skipped.
    MIN_PLAUSIBLE_FONT_SIZE = 2.0
    TITLE_MAX_CHARS = 300

    # A preprint's own stamps, not mentions: a published paper's first page
    # may cite "arXiv preprint arXiv:2101.00001" or link to osf.io for data.
    PREPRINT_PATTERNS = [
        r'arXiv:\d{4}\.\d{4,5}v\d+\s*\[[\w.-]+\]',      # margin stamp, with version
        r'\b(?:bio|med)Rxiv preprint\b',                  # page header
        r'(?im)^\s*preprint\b(?!s)',                       # "Preprint as of ...", "Preprint submitted to ..."
        r'\bpreprint\b[^.\n]{0,40}\bnot\b[^.\n]{0,20}\bpeer[- ]reviewed',
        r'ssrn\.com/abstract=',                            # SSRN download footer
        r'10\.31234/osf\.io/',                              # PsyArXiv / OSF Preprints DOI
        r'10\.21203/rs\.',                                  # Research Square DOI
    ]

    # arXiv's margin stamp: the ID with its version, then the category in
    # brackets. A citation ("arXiv preprint arXiv:2005.00002") has neither.
    ARXIV_STAMP = re.compile(
        r'arXiv:(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})v\d+\s*\[[\w.-]+\]')

    # Only labeled ISBNs: a bare 13-digit number may be an order or record
    # number. Digits may be split by hyphens or spaces; ISBN-10 may end in X.
    ISBN_PATTERN = re.compile(r'\bISBN(?:-1[03])?:?\s*((?:\d[-\s]?){9}[\dXx](?:[-\s]?\d){0,3})')
    XMP_ISBN_KEYS = ("}isbn",)   # prism:isbn (any version), pdfx:isbn

    def __init__(self, use_ocr: bool = True, ocr_pages: int = 1):
        """
        Initialize PDF processor.

        Args:
            use_ocr: Enable OCR fallback for scanned documents
            ocr_pages: Number of pages to OCR (default: 1)
        """
        self.use_ocr = use_ocr and OCR_AVAILABLE
        self.ocr_pages = ocr_pages

        if use_ocr and not OCR_AVAILABLE:
            log.warning("OCR unavailable: install the 'ocr' extra (pytesseract, pdf2image) "
                        "to read scanned PDFs.")

    def extract_metadata(self, pdf_path: str) -> PDFMetadata:
        """
        Extract metadata from a PDF file.

        Tries multiple methods in order:
        1. Standard text extraction (pypdf)
        2. OCR if text is insufficient (optional)
        3. Search for DOI in extracted text
        4. Extract title, authors, journal from text

        Args:
            pdf_path: Path to the PDF file

        Returns:
            PDFMetadata object with extracted information

        Raises:
            PDFNotFoundError: If PDF file doesn't exist
            PDFProcessingError: If PDF cannot be read
        """
        # Validate file exists
        if not os.path.exists(pdf_path):
            raise PDFNotFoundError(f"PDF file not found: {pdf_path}")

        log.debug("Processing: %s", pdf_path)

        # Initialize metadata object
        metadata = PDFMetadata()

        # Extract text from PDF
        text, chunks = self._extract_page(pdf_path)

        # If text is insufficient, try OCR
        if len(text.strip()) < 100:
            if self.use_ocr:
                log.debug("Little or no extractable text; using OCR")
                text = self._extract_text_with_ocr(pdf_path)
                metadata.used_ocr = True
            else:
                log.debug("Little or no extractable text and OCR is disabled")

        metadata.extracted_text = text
        metadata.preprint = any(re.search(p, text, re.IGNORECASE) for p in self.PREPRINT_PATTERNS)

        log.debug("Extracted %d characters of text", len(text))

        if text:
            title, authors, journal = self._extract_metadata_from_text(text)
            metadata.authors = authors
            metadata.journal = journal
            if title:
                metadata.title, metadata.title_source = title, "text"

            log.debug("Extracted title=%r authors=%r journal=%r", title, authors, journal)

        # The largest type on the page is the title far more often than the
        # line the order-and-capitalization guess picks (often a header).
        font_title = self._title_from_font_size(chunks)
        if font_title:
            metadata.title, metadata.title_source = font_title, "font size"

        metadata.doi_candidates = self._extract_dois(text)
        # The stamp says the PDF is the arXiv preprint, so its record comes
        # before a published version's DOI printed on the page.
        metadata.arxiv_id = self._extract_arxiv_stamp(text)
        if metadata.arxiv_id:
            stamp_doi = arxiv_doi(metadata.arxiv_id)
            metadata.doi_candidates = [stamp_doi] + [
                d for d in metadata.doi_candidates if d.lower() != stamp_doi.lower()]
        if metadata.doi_candidates:
            metadata.doi, metadata.doi_source = metadata.doi_candidates[0], "text"

        # What the publisher (or an earlier papertag run) embedded is more
        # reliable than guessing from page layout, so it overrides the text.
        embedded_doi, embedded_title = self._read_embedded_metadata(pdf_path, text)
        if embedded_doi:
            metadata.doi, metadata.doi_source = embedded_doi
        if embedded_title:
            # Some publishers embed the title cut at the colon; the page has it all.
            if not (font_title and self._extends(font_title, embedded_title[0])):
                metadata.title, metadata.title_source = embedded_title

        metadata.isbn_candidates = self._unique(
            self._read_embedded_isbns(pdf_path) + self._extract_isbns(text))

        log.debug("DOI=%r (from %s), title=%r (from %s)", metadata.doi, metadata.doi_source,
                  metadata.title, metadata.title_source)

        return metadata

    def _read_embedded_metadata(
        self, pdf_path: str, page_text: str = ""
    ) -> Tuple[Optional[Tuple[str, str]], Optional[Tuple[str, str]]]:
        """Read the DOI and title stored in the PDF's XMP and docinfo.

        Args:
            pdf_path: Path to the PDF file.
            page_text: Text of page 1. When there is enough of it, an embedded
                title is used only if the page shows it.

        Returns:
            ``(doi, title)``, each a ``(value, source)`` pair or None when the
            file has no usable value.
        """
        candidates_doi: List[Tuple[str, str]] = []
        candidates_title: List[Tuple[str, str]] = []
        try:
            with pikepdf.open(pdf_path) as pdf:
                xmp = self._read_xmp(pdf)
                docinfo = {str(k): v for k, v in pdf.docinfo.items()}
        except Exception as e:
            log.debug("Could not read embedded metadata from %s: %s", pdf_path, e)
            return None, None

        # A re-run must not trust what an earlier run wrote: a wrong batch pick
        # would be confirmed instead of fixed. papertag overwrites the title,
        # dc:identifier, /Subject and /Keywords, so in a file it has processed
        # only keys it never writes (publisher keys) are evidence.
        own_output = self._written_by_papertag(docinfo)
        if own_output:
            log.debug("%s was tagged by an earlier run of papertag; ignoring the title "
                      "and DOI that run wrote", pdf_path)

        for source, value in self._doi_fields(xmp, docinfo):
            if own_output and source not in self.PUBLISHER_DOI_SOURCES:
                continue
            doi = self._embedded_doi(value, labeled=source in self.LABELED_DOI_FIELDS)
            if doi:
                candidates_doi.append((doi, source))

        title_fields = () if own_output else (
            ("xmp dc:title", xmp.get(f"{{{self.DC_NS}}}title")),
            ("docinfo /Title", docinfo.get("/Title")),
        )
        for source, value in title_fields:
            title = self._clean_embedded_title(value)
            if not title:
                continue
            if not self._title_on_page(title, page_text):
                log.debug("Ignoring %s %r: not found in the page text", source, title)
                continue
            candidates_title.append((title, source))

        distinct = {doi.lower() for doi, _ in candidates_doi}
        if len(distinct) > 1:
            log.debug("Embedded DOIs disagree in %s, using the first: %s", pdf_path,
                      ", ".join(f"{doi} ({src})" for doi, src in candidates_doi))

        return (candidates_doi[0] if candidates_doi else None,
                candidates_title[0] if candidates_title else None)

    @staticmethod
    def _extends(longer: str, shorter: str) -> bool:
        """True if `longer` has more words than `shorter` and starts with all of them."""
        long_words = re.findall(r'\w+', longer.casefold())
        short_words = re.findall(r'\w+', shorter.casefold())
        return len(long_words) > len(short_words) and long_words[:len(short_words)] == short_words

    @staticmethod
    def _written_by_papertag(docinfo: dict) -> bool:
        """True if docinfo has the identifier layout MetadataUpdater writes.

        It writes /Keywords as exactly "DOI: <doi>" or "ISBN: <isbn>", and
        /Subject as "<journal> | DOI: <doi>" or "DOI: <doi>" (likewise ISBN).
        Older versions wrote only /Subject, so either one counts.
        """
        keywords = str(docinfo.get("/Keywords", ""))
        subject = str(docinfo.get("/Subject", ""))
        if re.fullmatch(r'(?:DOI|ISBN): \S+', keywords):
            return True
        return bool(re.search(r'(?:^| \| )(?:DOI|ISBN): \S+$', subject))

    @staticmethod
    def _read_xmp(pdf: "pikepdf.Pdf") -> dict:
        """Return XMP properties keyed ``{namespace-uri}name``; empty if unreadable."""
        try:
            with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
                return dict(meta.items())
        except Exception as e:
            log.debug("Unreadable XMP metadata: %s", e)
            return {}

    def _doi_fields(self, xmp: dict, docinfo: dict) -> Iterator[Tuple[str, object]]:
        """Yield ``(source, value)`` for every field that may hold the DOI, best first.

        Only named fields are read. Scanning every field for anything
        DOI-shaped would pick up e.g. Wiley's pdfx:WPS-JOURNALDOI, the
        journal's own DOI.
        """
        for key, value in xmp.items():
            if key.startswith(f"{{{self.PRISM_NS_PREFIX}") and key.endswith("}doi"):
                yield "xmp prism:doi", value
        for key, source in self.XMP_DOI_KEYS:
            if key in xmp:
                yield source, xmp[key]
        for key in self.DOCINFO_DOI_KEYS:
            if key in docinfo:
                yield f"docinfo {key}", docinfo[key]

    def _embedded_doi(self, value: object, labeled: bool) -> Optional[str]:
        """Extract a DOI from one metadata value.

        Args:
            value: The field value (string, or list/set for XMP arrays).
            labeled: For free-text fields such as /Subject: accept only a DOI
                introduced by "doi:", since a bare one may name another work.

        Returns:
            The DOI, or None if the value does not hold one.
        """
        values = value if isinstance(value, (list, set, tuple)) else [value]
        for item in values:
            text = str(item).strip()
            if labeled:
                match = re.search(r'\bdoi:?\s*(10\.\d{4,9}/[^\s"\'<>|]+)', text, re.IGNORECASE)
            else:
                text = re.sub(r'^(?:(?:https?://)?(?:www\.|dx\.)?doi\.org/'
                              r'|(?:urn:)?(?:info:)?doi[:/]\s*|doi\s+)', '',
                              text, flags=re.IGNORECASE)
                match = re.fullmatch(r'(10\.\d{4,9}/\S+)', text)
            if not match:
                continue
            # Not _sanitize_doi: that strips every trailing non-alphanumeric,
            # but a SICI DOI can end in its '#' check character. A structured
            # field holds just the DOI, so only sentence punctuation is stray.
            doi = match.group(1).rstrip('.,;')
            # "10.1111/(ISSN)1365-2486" identifies a journal, not an article.
            if "(issn)" in doi.lower():
                continue
            return doi
        return None

    def _title_on_page(self, title: str, page_text: str) -> bool:
        """True if most words of `title` appear in `page_text`, or there is too
        little text to tell (scanned page, OCR off).

        A title the page doesn't show was usually left behind by a template or
        authoring tool, which no junk list can enumerate.
        """
        if len(page_text.strip()) < 100:
            return True
        # Rejoin words hyphenated across lines ("Re-\ncharge").
        page = re.sub(r'-\s*\n\s*', '', page_text.lower())
        page_words = set(re.findall(r'\w+', page))
        title_words = [w for w in re.findall(r'\w+', title.lower()) if len(w) > 2]
        if not title_words:
            return True
        found = sum(w in page_words for w in title_words)
        return found / len(title_words) >= self.TITLE_ON_PAGE_MIN_SHARE

    def _clean_embedded_title(self, value: object) -> Optional[str]:
        """Return a usable title from a /Title or dc:title value, or None for junk.

        Many PDFs carry a title set by the authoring tool rather than the
        author ("Microsoft Word - draft.docx", "untitled", a filename). Sending
        that to Crossref is worse than using the page-text heuristic.
        """
        if value is None:
            return None
        title = html.unescape(re.sub(r'<[^>]+>', '', str(value)))
        title = " ".join(title.split())
        if len(title) < 6 or len(title.split()) < 2:
            return None
        if any(re.search(pattern, title, re.IGNORECASE) for pattern in self.JUNK_TITLE_PATTERNS):
            return None
        letters = sum(c.isalpha() for c in title)
        if letters < len(title) / 2:
            return None
        return title

    def _extract_text(self, pdf_path: str) -> str:
        """Extract the text layer of the first page (see ``_extract_page``)."""
        return self._extract_page(pdf_path)[0]

    def _extract_page(self, pdf_path: str) -> Tuple[str, List[TextChunk]]:
        """
        Extract the text layer of the first page, with the size of each piece.

        Only page 1 is read: it carries the title, authors, and usually the
        DOI, and reading further slows down large PDFs for no gain.

        Args:
            pdf_path: Path to PDF file

        Returns:
            ``(text, chunks)``: the page text (empty if the page has no text
            layer) and the pieces it was drawn in, in content-stream order.

        Raises:
            PDFReadError: If the PDF cannot be parsed
        """
        chunks: List[TextChunk] = []

        def visit(text: str, cm: list, tm: list, font: object, font_size: float) -> None:
            chunks.append(self._text_chunk(text, cm, tm, font_size))

        try:
            reader = PdfReader(pdf_path)
            if len(reader.pages) == 0:
                raise PDFReadError("PDF has no pages")
            text = reader.pages[0].extract_text(visitor_text=visit) or ""
            return text, chunks
        except PDFReadError:
            raise
        except Exception as e:
            raise PDFReadError(f"Failed to read PDF: {e}") from e

    @staticmethod
    def _text_chunk(text: str, cm: list, tm: list, font_size: float) -> TextChunk:
        """Build a TextChunk from what pypdf passes its text visitor.

        The rendered size is the Tf size scaled by the text and current
        transformation matrices; publishers often set Tf to 1 and scale.
        """
        # m = tm x cm, keeping the parts that tell size and rotation.
        m1 = tm[0] * cm[1] + tm[1] * cm[3]
        m2 = tm[2] * cm[0] + tm[3] * cm[2]
        m3 = tm[2] * cm[1] + tm[3] * cm[3]
        if not text.strip():
            return TextChunk(text, None)
        rotated = abs(m1) > 1e-3 or abs(m2) > 1e-3 or m3 <= 0
        return TextChunk(text, font_size * math.hypot(m2, m3), rotated)

    def _title_from_font_size(self, chunks: List[TextChunk]) -> Optional[str]:
        """Return the text set in the largest type on the page, if it looks like a title.

        Candidates are blocks of one size larger than the body text, with at
        least three words. Among the largest (and those within 5% of it) the
        longest wins: it is the title, not a journal name or a label.

        Args:
            chunks: Page-1 text pieces from ``_extract_page``.

        Returns:
            The title, or None when no block qualifies (e.g. a page set in
            one size, or a scan without a text layer).
        """
        blocks = self._text_blocks(chunks)
        letters_by_size: Dict[float, int] = {}
        for block in blocks:
            letters = sum(c.isalpha() for c in block.text)
            size = round(block.size, 1)
            letters_by_size[size] = letters_by_size.get(size, 0) + letters
        if not letters_by_size:
            return None
        body_size = max(letters_by_size, key=letters_by_size.get)

        candidates = [b for b in blocks
                      if b.size > body_size + self.FONT_SIZE_TOLERANCE
                      and self._looks_like_title(b.text)]
        if not candidates:
            return None
        largest = max(b.size for b in candidates)
        near = [b for b in candidates if b.size >= largest * self.FONT_SIZE_NEAR_RATIO]
        best = max(near, key=lambda b: sum(c.isalpha() for c in b.text))
        return best.text

    def _text_blocks(self, chunks: List[TextChunk]) -> List[_TextBlock]:
        """Group chunks into runs of one font size, in drawing order.

        Position is not used: pypdf reports a wrong height for lines after a
        line break in some publishers' PDFs. Different text drawn in between
        (a cover page's small print) is what separates two blocks.
        """
        blocks: List[_TextBlock] = []
        current: Optional[_TextBlock] = None
        for chunk in chunks:
            if chunk.rotated:
                continue
            if chunk.size is None:  # whitespace: belongs to the block it is in
                if current:
                    current.parts.append(chunk.text)
                continue
            if chunk.size < self.MIN_PLAUSIBLE_FONT_SIZE:
                continue
            if current is not None and abs(chunk.size - current.size) <= self.FONT_SIZE_TOLERANCE:
                current.parts.append(chunk.text)
                continue
            # A footnote marker (*, 1, a dagger) set in smaller type inside a
            # title must not split it.
            if current is not None and not any(c.isalpha() for c in chunk.text) \
                    and len(chunk.text.strip()) <= 3:
                continue
            current = _TextBlock(chunk.size, [chunk.text])
            blocks.append(current)
        return blocks

    def _looks_like_title(self, text: str) -> bool:
        """True if a block of large text could be a title."""
        words = [w for w in text.split() if any(c.isalpha() for c in w)]
        if len(words) < self.TITLE_MIN_WORDS or len(text) > self.TITLE_MAX_CHARS:
            return False
        return not any(re.search(pattern, text, re.IGNORECASE)
                       for pattern in self.TITLE_BLACKLIST_PATTERNS)

    def _extract_text_with_ocr(self, pdf_path: str, pages: Optional[int] = None) -> str:
        """
        Extract text from PDF using OCR.

        Args:
            pdf_path: Path to PDF file
            pages: Number of pages to OCR (default: self.ocr_pages)

        Returns:
            Extracted text from OCR

        Raises:
            OCRNotAvailableError: If OCR libraries are not available
        """
        if not OCR_AVAILABLE:
            raise OCRNotAvailableError("OCR libraries (pytesseract, pdf2image) are not installed")

        if pages is None:
            pages = self.ocr_pages

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                # Convert PDF pages to images
                images = convert_from_path(
                    pdf_path,
                    first_page=1,
                    last_page=pages,
                    dpi=300,
                    output_folder=temp_dir
                )

                # Extract text from each image using OCR
                text = ""
                for i, image in enumerate(images):
                    log.debug("OCR page %d/%d", i + 1, len(images))
                    page_text = pytesseract.image_to_string(image)
                    text += f"\n--- Page {i+1} ---\n{page_text}"

                return text

        except Exception as e:
            log.warning("OCR failed for %s: %s", pdf_path, e)
            return ""

    def _sanitize_doi(self, doi: str) -> str:
        """
        Sanitize DOI by removing trailing periods and invalid characters.

        Args:
            doi: Raw DOI string

        Returns:
            Sanitized DOI
        """
        doi = doi.strip()
        # Remove trailing non-alphanumeric characters
        doi = re.sub(r'[^a-zA-Z0-9]+$', '', doi)
        return doi

    def _extract_doi(self, text: str) -> Optional[str]:
        """Return the best DOI in `text` (see _extract_dois), or None."""
        dois = self._extract_dois(text)
        return dois[0] if dois else None

    def _extract_dois(self, text: str) -> List[str]:
        """Every distinct DOI in `text`, repaired, best first.

        Labeled DOIs ("doi: ...") come first, then doi.org links, then bare
        ones, each in page order. A later copy with a glued-on word or URL path
        is dropped. A later, shorter copy is kept after the earlier one but
        never replaces it: it is either the clean copy of a damaged DOI or a
        DOI cut where a line wrapped, and only a lookup can tell which.

        Args:
            text: Page text.

        Returns:
            DOIs, possibly empty.
        """
        text = self.DOI_LINE_BREAK.sub(r'\1', text)
        dois: List[str] = []
        for pattern in self.DOI_PATTERNS:
            for match in re.finditer(pattern, text):
                doi = re.sub(r'^doi\.org/', '', match.group(1))
                doi = self.DOI_SUPPLEMENT_SUFFIX.sub('', self._sanitize_doi(doi))
                self._add_doi(dois, doi)
        return dois

    def _extract_arxiv_stamp(self, text: str) -> Optional[str]:
        """The arXiv ID (without version) in page 1's margin stamp, or None."""
        match = self.ARXIV_STAMP.search(text)
        return match.group(1) if match else None

    def _extract_isbns(self, text: str) -> List[str]:
        """Labeled, checksum-valid ISBNs in `text` as ISBN-13s, in page order."""
        found = []
        for match in self.ISBN_PATTERN.finditer(text):
            isbn = isbn13(match.group(1))
            if not isbn:
                # The pattern may run on into a number after an ISBN-10 (a year).
                digits = re.findall(r'[\dXx]', match.group(1))
                isbn = isbn13(''.join(digits[:10]))
            if isbn:
                found.append(isbn)
        return self._unique(found)

    def _read_embedded_isbns(self, pdf_path: str) -> List[str]:
        """ISBN-13s in the publisher's XMP keys (prism:isbn, pdfx:isbn).

        papertag itself writes ISBNs only to /Keywords, /Subject and
        dc:identifier, so these keys are never an earlier run's guess.
        """
        try:
            with pikepdf.open(pdf_path) as pdf:
                xmp = self._read_xmp(pdf)
        except Exception as e:
            log.debug("Could not read XMP from %s: %s", pdf_path, e)
            return []
        values = [v for k, v in xmp.items() if k.lower().endswith(self.XMP_ISBN_KEYS)]
        found = (isbn13(str(v)) for v in values)
        return self._unique(isbn for isbn in found if isbn)

    @staticmethod
    def _unique(items: Iterable[str]) -> List[str]:
        """`items` without repeats, first occurrence kept."""
        return list(dict.fromkeys(items))

    def _add_doi(self, dois: List[str], doi: str) -> None:
        """Add `doi` to `dois` unless it repeats one already there."""
        lower = doi.lower()
        for known in dois:
            known_lower = known.lower()
            if lower == known_lower or self._is_damaged_copy(lower, known_lower):
                return
        dois.append(doi)

    def _is_damaged_copy(self, longer: str, shorter: str) -> bool:
        """True if `longer` is `shorter` with a glued-on word or URL path."""
        return (longer.startswith(shorter) and len(longer) > len(shorter)
                and bool(self.DOI_DAMAGE.match(longer[len(shorter)])))

    def _extract_metadata_from_text(self, text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Extract title, authors, and journal from PDF text.

        Args:
            text: Extracted text from PDF

        Returns:
            Tuple of (title, authors, journal)
        """
        # Split into lines and filter out short lines
        lines = [line.strip() for line in text.split('\n') if len(line.strip()) > 3]

        # Filter out headers, footers, and copyright notices
        filtered_lines = []
        for line in lines:
            # Skip lines that match blacklist patterns
            if any(re.search(pattern, line, re.IGNORECASE) for pattern in self.TITLE_BLACKLIST_PATTERNS):
                continue
            filtered_lines.append(line)

        # Extract title
        title = self._extract_title(filtered_lines)

        # Extract authors
        authors = self._extract_authors(lines, title)

        # Extract journal
        journal = self._extract_journal(lines)

        return title, authors, journal

    def _extract_title(self, filtered_lines: list) -> Optional[str]:
        """
        Extract title from filtered lines.

        Args:
            filtered_lines: Lines with blacklisted content removed

        Returns:
            Title if found, None otherwise
        """
        title_candidates = []

        # Look for lines with typical title characteristics
        for line in filtered_lines[:min(30, len(filtered_lines))]:
            # Skip very short lines and lines starting with numbers
            if len(line) < 10 or re.match(r'^\d', line):
                continue

            # Check for lines that might be titles (uppercase or title case)
            if line.isupper() or sum(1 for c in line if c.isupper()) >= 2:
                if len(line) < 200:  # Title shouldn't be too long
                    title_candidates.append((line, filtered_lines.index(line)))

        # Sort candidates by position (earlier in document more likely to be title)
        title_candidates.sort(key=lambda x: x[1])

        if title_candidates:
            # Prefer all-caps titles, which are common in academic papers
            all_caps_candidates = [t for t, idx in title_candidates if t.isupper()]
            if all_caps_candidates:
                return all_caps_candidates[0]
            else:
                return title_candidates[0][0]

        return None

    def _extract_authors(self, lines: list, title: Optional[str]) -> Optional[str]:
        """
        Extract authors from lines.

        Args:
            lines: All extracted lines
            title: Extracted title (used to find author position)

        Returns:
            Authors if found, None otherwise
        """
        if not title:
            return None

        # Find title position
        title_idx = -1
        for i, line in enumerate(lines):
            if title in line:
                title_idx = i
                break

        if title_idx < 0:
            return None

        # Look at the next few lines for author information
        for i in range(title_idx + 1, min(title_idx + 10, len(lines))):
            # Author lines often contain names (capitalized words)
            name_pattern = r'([A-Z][a-z]+ [A-Z][a-z]+)'
            if re.search(name_pattern, lines[i]) and not lines[i].isupper():
                # Check it's not a blacklisted line
                if not any(re.search(pattern, lines[i], re.IGNORECASE)
                          for pattern in self.TITLE_BLACKLIST_PATTERNS):
                    return lines[i]

        return None

    def _extract_journal(self, lines: list) -> Optional[str]:
        """
        Extract journal name from lines.

        Args:
            lines: All extracted lines

        Returns:
            Journal name if found, None otherwise
        """
        for line in lines:
            journal_match = re.search(
                r'(Journal of|Proceedings of|Transactions of|Review of).*',
                line,
                re.IGNORECASE
            )
            if journal_match:
                return journal_match.group(0)

        return None
