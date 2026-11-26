"""
PDF Processor Module

Extracts text, metadata, and DOIs from PDF files with OCR fallback for scanned documents.
"""

import html
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

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


class PDFProcessor:
    """Extract text and metadata from PDF files."""

    # DOI detection patterns
    DOI_PATTERNS = [
        r'(?:doi|DOI):?\s*(10\.\d{4,9}/[^\s"\'<>]+)',
        r'https?://doi\.org/(10\.\d{4,9}/[^\s"\'<>]+)',
        r'https?://(doi\.org/10\.\d{4,9}/[^\s"\'<>]+)',
        r'(?:^|\s|[^\w.])(10\.\d{4,9}/[^\s"\'<>]+)(?:$|\s|[^\w.])',
        r'doi\.org/(10\.\d{4,9}/[^\s"\'<>]+)'
    ]

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
        r'Elsevier|Springer|Wiley|SAGE|IEEE|Oxford University Press|Cambridge University Press'
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
        text = self._extract_text(pdf_path)

        # If text is insufficient, try OCR
        if len(text.strip()) < 100:
            if self.use_ocr:
                log.debug("Little or no extractable text; using OCR")
                text = self._extract_text_with_ocr(pdf_path)
                metadata.used_ocr = True
            else:
                log.debug("Little or no extractable text and OCR is disabled")

        metadata.extracted_text = text

        log.debug("Extracted %d characters of text", len(text))

        if text:
            title, authors, journal = self._extract_metadata_from_text(text)
            metadata.authors = authors
            metadata.journal = journal
            if title:
                metadata.title, metadata.title_source = title, "text"

            log.debug("Extracted title=%r authors=%r journal=%r", title, authors, journal)

        text_doi = self._extract_doi(text)
        if text_doi:
            metadata.doi, metadata.doi_source = text_doi, "text"

        # What the publisher (or an earlier papertag run) embedded is more
        # reliable than guessing from page layout, so it overrides the text.
        embedded_doi, embedded_title = self._read_embedded_metadata(pdf_path, text)
        if embedded_doi:
            metadata.doi, metadata.doi_source = embedded_doi
        if embedded_title:
            metadata.title, metadata.title_source = embedded_title

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
        """
        Extract the text layer of the first page.

        Only page 1 is read: it carries the title, authors, and usually the
        DOI, and reading further slows down large PDFs for no gain.

        Args:
            pdf_path: Path to PDF file

        Returns:
            Extracted text (empty if the page has no text layer)

        Raises:
            PDFReadError: If the PDF cannot be parsed
        """
        try:
            reader = PdfReader(pdf_path)
            if len(reader.pages) == 0:
                raise PDFReadError("PDF has no pages")
            return reader.pages[0].extract_text() or ""
        except PDFReadError:
            raise
        except Exception as e:
            raise PDFReadError(f"Failed to read PDF: {e}") from e

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
        """
        Extract DOI from text using multiple patterns.

        Args:
            text: Text to search for DOI

        Returns:
            DOI if found, None otherwise
        """
        for pattern in self.DOI_PATTERNS:
            match = re.search(pattern, text)
            if match:
                # Extract the DOI
                doi = match.group(1)

                # Remove "doi.org/" prefix if present
                if "doi.org/" in doi:
                    doi = re.sub(r'doi\.org/', '', doi)

                doi = self._sanitize_doi(doi)
                return doi

        return None

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
