#!/usr/bin/env python3
"""
papertag - command-line entry point and per-file orchestrator

Update PDF metadata from academic sources like Crossref, with interactive
or batch processing modes.
"""

import argparse
import dataclasses
import logging
import re
import os
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

from papertag import __version__
from papertag.core import (
    PDFProcessor,
    PDFMetadata,
    PDFProcessingError,
    PDFNotFoundError,
    CrossrefClient,
    CrossrefMatch,
    CrossrefConnectionError,
    CrossrefAPIError,
    MetadataUpdater,
    MetadataUpdate,
    PDFUpdateError,
    FileOperationError,
    parse_filename,
    FilenameHints
)
from papertag.core.crossref_client import BATCH_THRESHOLD, batch_choice
from papertag.core.pdf_processor import ThesisInfo, arxiv_doi
from papertag.ui import InteractiveUI, UserQuitError
from papertag.utils import SessionLogger

log = logging.getLogger(__name__)

# Filename patterns this confident ("Author - Year - Title", "Author (Year) Title")
# hold a title and author worth more than line-order guesses from the page.
FILENAME_TRUSTED = 0.85


# Page-1 DOIs looked up per file before giving up and searching. A page with
# more than one DOI is uncommon (22 of 400 sampled), and each lookup is a request.
MAX_PAGE_DOI_LOOKUPS = 3


def lookup_doi(client: CrossrefClient, pdf_metadata: PDFMetadata,
               rejected: Optional[List[CrossrefMatch]] = None,
               filename_hints: Optional[FilenameHints] = None) -> Optional[CrossrefMatch]:
    """Fetch the record for the PDF's own DOI, or None to search instead.

    An embedded DOI (put there by the publisher) is trusted. DOIs read from
    page 1 are tried best first, and a record counts only if its title is on
    the page (CrossrefClient.shown_on_page): the first DOI on a page can be a
    cited work's or the supplement's. An arXiv filename's DOI is tried last,
    with the same check.

    Args:
        client: Crossref client.
        pdf_metadata: What was extracted from the PDF.
        rejected: If given, records that failed the page check are appended
            with score 0.0. A damaged text layer can fail the paper's own
            record, so interactive mode still offers them.
        filename_hints: What the filename says; its arXiv ID, if any, is
            looked up after the page's DOIs.

    Returns:
        The record, with score 1.0, or None. Crossref errors are logged, not
        raised: the caller then searches, which reports a network failure.
    """
    embedded = pdf_metadata.doi if pdf_metadata.doi_source != "text" else None
    if embedded:
        try:
            return client.fetch_metadata(embedded)
        except CrossrefConnectionError as e:
            log.warning("Lookup of DOI %s failed: %s", embedded, e)
            return None
        except CrossrefAPIError as e:
            log.warning("Lookup of DOI %s failed: %s", embedded, e)

    page_dois = list(pdf_metadata.doi_candidates)
    if not page_dois and pdf_metadata.doi and not embedded:
        page_dois = [pdf_metadata.doi]
    if filename_hints and filename_hints.arxiv_id:
        # After the page's DOIs: the page says what the PDF is, the filename
        # only what it was saved as (a published version may keep the name).
        page_dois.append(arxiv_doi(filename_hints.arxiv_id))
    page_dois = [d for d in page_dois if not embedded or d.lower() != embedded.lower()]
    unique: List[str] = []
    for doi in page_dois:
        if doi.lower() not in {d.lower() for d in unique}:
            unique.append(doi)
    page_dois = unique
    for doi in page_dois[:MAX_PAGE_DOI_LOOKUPS]:
        try:
            match = client.fetch_metadata(doi)
        except CrossrefConnectionError as e:
            log.warning("Lookup of DOI %s failed: %s", doi, e)
            return None
        except CrossrefAPIError as e:
            # Usually a DOI damaged by text extraction.
            log.info("DOI %s from page 1 not found in Crossref: %s", doi, e)
            continue
        if client.shown_on_page(match, pdf_metadata.extracted_text):
            return match
        log.info("DOI %s on page 1 is for another work (%r); not using it", doi, match.title)
        if rejected is not None:
            match.score = 0.0
            rejected.append(match)
    if len(page_dois) > MAX_PAGE_DOI_LOOKUPS:
        log.info("Tried %d of %d DOIs on page 1", MAX_PAGE_DOI_LOOKUPS, len(page_dois))
    return None


def lookup_isbn(client: CrossrefClient, pdf_metadata: PDFMetadata,
                title: Optional[str] = None) -> Optional[CrossrefMatch]:
    """Find the record of a PDF that carries an ISBN, or None to search instead.

    Tried when no DOI resolved. The ISBN is the book's, so the record is the
    one of that book's whose title is the page's (CrossrefClient.isbn_record).

    Args:
        client: Crossref client.
        pdf_metadata: What was extracted from the PDF.
        title: Title to choose by; defaults to the page's.

    Returns:
        The record, with score 1.0, or None. Crossref errors are logged, not
        raised: the caller then searches, which reports a network failure.
    """
    title = title or pdf_metadata.title
    if not title:
        return None
    for isbn in pdf_metadata.isbn_candidates:
        try:
            match = client.isbn_record(isbn, title, pdf_metadata.extracted_text)
        except (CrossrefConnectionError, CrossrefAPIError) as e:
            log.warning("Lookup of ISBN %s failed: %s", isbn, e)
            return None
        if match:
            return match
        log.info("No record of ISBN %s has the page's title; searching instead", isbn)
    return None


def thesis_journal(match: CrossrefMatch, thesis: Optional[ThesisInfo]) -> CrossrefMatch:
    """Give a dissertation record "<degree>, <institution>" as its journal.

    Registries file the institution, or its library, as the publisher; the
    title page names the degree and the university itself.

    Args:
        match: A candidate record.
        thesis: What the PDF's title page says, if it is a thesis.

    Returns:
        `match` unchanged unless it is a dissertation record; else a copy with
        the label, using the page's institution when it names one.
    """
    if match.work_type != 'dissertation':
        return match
    degree = thesis.degree if thesis else "Dissertation"
    institution = (thesis.institution if thesis else None) or match.journal
    journal = f"{degree}, {institution}" if institution else degree
    return dataclasses.replace(match, journal=journal)


SMALL_TITLE_WORDS = {'a', 'an', 'and', 'as', 'at', 'by', 'for', 'from', 'in', 'into', 'of',
                     'on', 'or', 'the', 'to', 'with'}


def _title_case(title: str) -> str:
    """"GLACIAL VALLEYS OF THE HIGHLANDS" -> "Glacial Valleys of the Highlands"."""
    words = title.title().split(' ')
    return ' '.join(w.lower() if i and w.lower() in SMALL_TITLE_WORDS else w
                    for i, w in enumerate(words))


def title_page_record(pdf_metadata: PDFMetadata,
                      filename_hints: FilenameHints) -> Optional[CrossrefMatch]:
    """A record read off a thesis title page, for when no registry has it.

    Never checked against anything, so it is offered at score 0.0 and only
    in interactive mode, where the user sees it before choosing it.

    Args:
        pdf_metadata: What was extracted; its `thesis` must be set.
        filename_hints: A well-formed filename's author fills a missing "by" line.

    Returns:
        The record (no DOI, type dissertation), or None without a title.
    """
    thesis = pdf_metadata.thesis
    # The title search would use: a line-order guess loses to a good filename.
    title = search_query(pdf_metadata, filename_hints).title
    if not thesis or not title:
        return None
    title = re.sub(r'\s+', ' ', title).strip()
    if title.isupper():
        title = _title_case(title)
    author = thesis.author
    if not author and filename_hints.confidence >= FILENAME_TRUSTED:
        author = filename_hints.author
    journal = f"{thesis.degree}, {thesis.institution}" if thesis.institution else thesis.degree
    return CrossrefMatch(doi='', title=title, authors=[author] if author else [],
                         year=thesis.year or pdf_metadata.year, journal=journal, score=0.0,
                         work_type='dissertation')


class SearchQuery(NamedTuple):
    """What papertag searches Crossref with.

    author_reliable is False when the author is the page-text guess, which
    held the right name for about one file in six; Crossref then uses it to
    rank, not to match authors. preprint is True when the PDF carries a
    preprint server's stamp, so a tie goes to the preprint record.
    """
    title: Optional[str]
    author: Optional[str]
    year: Optional[str]
    author_reliable: bool
    preprint: bool = False


def search_query(pdf_metadata: PDFMetadata, filename_hints: FilenameHints) -> SearchQuery:
    """Choose the title, author and year to search Crossref with.

    Shared with tools/eval_library.py so it measures the query papertag sends.

    Args:
        pdf_metadata: What was read from the PDF (embedded metadata, page text).
        filename_hints: What was parsed from the filename.

    Returns:
        The query; title, author and year are None when neither source has them.
    """
    trusted = filename_hints.confidence >= FILENAME_TRUSTED
    title = pdf_metadata.title or filename_hints.title
    # A title guessed from line order is often a running header ("ORIGINAL
    # ARTICLE"); a title from a well-formed filename ("Author - Year - Title")
    # is usually right. Embedded and font-size titles are better than either.
    if pdf_metadata.title_source == "text" and filename_hints.title and trusted:
        title = filename_hints.title
    # The page author is always a line-order guess, and on a real library it
    # held the right name for about one file in six.
    if filename_hints.author and (trusted or not pdf_metadata.authors):
        author, author_reliable = filename_hints.author, True
    else:
        author, author_reliable = pdf_metadata.authors, False
    return SearchQuery(title, author, pdf_metadata.year or filename_hints.year,
                       author_reliable=author_reliable, preprint=pdf_metadata.preprint)


class PDFMetadataManager:
    """Main orchestrator for PDF metadata processing."""

    def __init__(
        self,
        email: Optional[str] = None,
        use_ocr: bool = True,
        ocr_pages: int = 1,
        keep_backup: bool = False,
        retries: int = 3,
        verbose: bool = False,
        quiet: bool = False,
        batch_mode: bool = False,
        rename: bool = True,
        log_path: Optional[str] = None,
        from_filename: bool = False,
        threshold: float = BATCH_THRESHOLD,
    ):
        """
        Initialize the orchestrator.

        Args:
            email: Email for Crossref API (polite pool), not required for --from-filename
            use_ocr: Enable OCR fallback for scanned documents
            ocr_pages: Number of pages to OCR (default: 1)
            keep_backup: Keep .bak copy of original files
            retries: Number of retry attempts for Crossref API
            verbose: Show detailed information
            quiet: Minimal output (errors and summary only)
            batch_mode: Auto-accept high-confidence matches
            rename: Rename files to Zotero format
            log_path: Path to write a JSON session log to (default: no log file)
            from_filename: Extract metadata from Zotero-style filename (skip Crossref)
            threshold: Minimum score for batch mode to apply a search result
        """
        self.batch_mode = batch_mode
        self.threshold = threshold
        self.rename = rename
        self.from_filename = from_filename

        # Initialize components
        self.pdf_processor = PDFProcessor(
            use_ocr=use_ocr,
            ocr_pages=ocr_pages,
        )
        # Only initialize Crossref client if not using from_filename mode
        if not from_filename and email:
            self.crossref_client = CrossrefClient(
                email=email,
                retries=retries
            )
        else:
            self.crossref_client = None
        self.metadata_updater = MetadataUpdater(keep_backup=keep_backup)
        self.ui = InteractiveUI(verbose=verbose, quiet=quiet)

        # Settings for logging
        settings = {
            'use_ocr': use_ocr,
            'ocr_pages': ocr_pages,
            'keep_backup': keep_backup,
            'batch_mode': batch_mode,
            'threshold': threshold,
            'rename': rename,
            'retries': retries
        }
        self.logger = SessionLogger(log_path=log_path, settings=settings)

        # Statistics
        self.stats = {
            'completed': 0,
            'skipped': 0,
            'failed': 0
        }

    def process_single_pdf(self, pdf_path: str) -> str:
        """
        Process a single PDF file.

        Args:
            pdf_path: Path to the PDF file

        Returns:
            'completed', 'skipped', or 'failed'
        """
        try:
            # Convert to absolute path to avoid issues with relative paths
            pdf_path = str(Path(pdf_path).resolve())

            if not self.ui.quiet:
                print(f"\n{'='*60}")
                print(f"Processing: {Path(pdf_path).name}")
                print(f"{'='*60}")

            # Step 1: Parse filename hints
            filename_hints = parse_filename(Path(pdf_path).name)

            log.debug("Filename hints: author=%s year=%s confidence=%.2f",
                      filename_hints.author, filename_hints.year, filename_hints.confidence)

            # Handle --from-filename mode: extract metadata from filename only
            if self.from_filename:
                return self._process_from_filename(pdf_path, filename_hints)

            # Step 2: Extract PDF metadata
            if not self.ui.quiet:
                print("Extracting PDF metadata...")

            pdf_metadata = self.pdf_processor.extract_metadata(pdf_path)

            log.debug("Extracted DOI=%s title=%s used_ocr=%s",
                      pdf_metadata.doi, pdf_metadata.title, pdf_metadata.used_ocr)

            # Display current PDF docinfo metadata (same fields we will update)
            if not self.ui.quiet:
                current = self.metadata_updater.read_metadata(pdf_path)
                print("\nCurrent PDF metadata:")
                print(f"  Title:   {current.title or '(none)'}")
                print(f"  Authors: {current.authors or '(none)'}")
                print(f"  Year:    {current.year or '(none)'}")
                print(f"  Journal: {current.journal or '(none)'}")
                print(f"  DOI:     {current.doi or '(none)'}")
                if current.isbn:
                    print(f"  ISBN:    {current.isbn}")
                print()

            # Step 3: Search Crossref
            if not self.ui.quiet:
                print("Searching Crossref API...")

            query = search_query(pdf_metadata, filename_hints)

            rejected: List[CrossrefMatch] = []
            doi_match = (lookup_doi(self.crossref_client, pdf_metadata, rejected=rejected,
                                    filename_hints=filename_hints)
                         or lookup_isbn(self.crossref_client, pdf_metadata, title=query.title))
            if doi_match:
                matches = [doi_match]
            else:
                matches = self._search(query)
                if pdf_metadata.thesis:
                    matches = self._add_datacite_theses(matches, query)
                # Last, at 0.00: never auto-accepted, but there to pick.
                found = {m.doi.lower() for m in matches}
                matches += [m for m in rejected if m.doi.lower() not in found]
                if not self.batch_mode:
                    record = title_page_record(pdf_metadata, filename_hints)
                    if record:
                        matches.append(record)
            matches = [thesis_journal(m, pdf_metadata.thesis) for m in matches]

            if not matches and self.batch_mode:
                if not self.ui.quiet:
                    print("No matches found in Crossref")
                self.logger.log_skip(pdf_path, "No Crossref matches found")
                return 'skipped'
            elif not matches and not self.ui.quiet:
                print("No matches found in Crossref")

            # Step 4: Select match (batch or interactive)
            selected_match = None
            # Whether the user picked a numbered candidate directly from the
            # list (as opposed to entering a manual DOI). A direct pick is
            # applied immediately, without a separate confirmation step.
            selected_directly = False

            if self.batch_mode:
                decision = batch_choice(matches, threshold=self.threshold)
                if decision.match is None:
                    if not self.ui.quiet:
                        print(f"Skipped: {decision.reason}")
                    self.logger.log_skip(pdf_path, decision.reason)
                    return 'skipped'
                selected_match = decision.match
                if not self.ui.quiet:
                    print(f"Auto-selected (score: {selected_match.score:.2f})")
            else:
                # Interactive mode
                selected_match = self.ui.display_matches(
                    matches,
                    Path(pdf_path).name,
                    filename_hints
                )

                # A CrossrefMatch here means the user chose a numbered option.
                # A title-page record (no DOI) was checked against nothing and
                # may lack an author, so it is confirmed like a typed DOI.
                selected_directly = (isinstance(selected_match, CrossrefMatch)
                                     and bool(selected_match.doi))

                # Handle special return values from display_matches
                if selected_match is None:
                    self.logger.log_skip(pdf_path, "User skipped")
                    return 'skipped'
                elif selected_match == 'retry':
                    # User requested retry - recursive call
                    return self.process_single_pdf(pdf_path)
                elif selected_match == 'from_filename':
                    # User chose to use metadata from filename
                    return self._process_from_filename(pdf_path, filename_hints)
                elif isinstance(selected_match, tuple) and selected_match[0] == 'manual':
                    # User entered manual DOI
                    manual_doi = selected_match[1]
                    try:
                        selected_match = self.crossref_client.fetch_metadata(manual_doi)
                    except (CrossrefConnectionError, CrossrefAPIError) as e:
                        log.error("Failed to fetch metadata for DOI %s: %s", manual_doi, e)
                        self.logger.log_failure(pdf_path, f"Invalid DOI: {manual_doi}")
                        return 'failed'

            # Step 5: Prepare metadata update
            metadata_update = self._match_to_metadata_update(selected_match)

            # Generate new filename
            if self.rename:
                new_filename = self.metadata_updater.generate_zotero_filename(
                    metadata_update,
                    pdf_path
                )
            else:
                new_filename = Path(pdf_path).name

            # Step 6: Confirm (skip for a direct numbered pick; the user has
            # already seen the candidate's details in the list)
            if not self.batch_mode and not selected_directly:
                confirmed = self.ui.confirm_metadata(
                    Path(pdf_path).name,
                    metadata_update,
                    new_filename
                )

                if not confirmed:
                    self.logger.log_skip(pdf_path, "User declined metadata")
                    return 'skipped'

            # Step 7: Update PDF metadata
            if not self.ui.quiet:
                print("Updating PDF metadata...")

            self.metadata_updater.update_metadata(pdf_path, metadata_update)

            # Step 8: Rename file
            new_path = pdf_path
            if self.rename:
                if not self.ui.quiet:
                    print(f"Renaming to: {new_filename}")

                new_path = self.metadata_updater.rename_file(
                    pdf_path,
                    new_filename
                )

            # Step 9: Log success
            self.logger.log_success(
                original_path=pdf_path,
                new_path=new_path,
                doi=selected_match.doi or None,
                confidence=selected_match.score,
                used_ocr=pdf_metadata.used_ocr
            )

            if not self.ui.quiet:
                print("Done")

            return 'completed'

        except UserQuitError:
            raise  # Propagate quit signal

        except PDFNotFoundError as e:
            log.error("%s", e)
            self.logger.log_failure(pdf_path, str(e))
            return 'failed'

        except (PDFProcessingError, PDFUpdateError, FileOperationError) as e:
            # Handle errors interactively
            if self.batch_mode:
                log.error("%s: %s", Path(pdf_path).name, e)
                self.logger.log_failure(pdf_path, str(e))
                return 'failed'
            else:
                choice = self.ui.handle_error(
                    Path(pdf_path).name,
                    e,
                    retryable=False
                )

                if choice == 'quit':
                    raise UserQuitError()

                self.logger.log_failure(pdf_path, str(e))
                return 'failed'

        except (CrossrefConnectionError, CrossrefAPIError) as e:
            # Connection errors can be retried
            if self.batch_mode:
                log.error("%s: Crossref error: %s", Path(pdf_path).name, e)
                self.logger.log_failure(pdf_path, str(e))
                return 'failed'
            else:
                choice = self.ui.handle_error(
                    Path(pdf_path).name,
                    e,
                    retryable=True
                )

                if choice == 'retry':
                    # Recursive retry
                    return self.process_single_pdf(pdf_path)
                elif choice == 'quit':
                    raise UserQuitError()
                else:
                    self.logger.log_failure(pdf_path, str(e))
                    return 'failed'

        except Exception as e:
            log.error("%s: unexpected error: %s", Path(pdf_path).name, e)
            log.debug("Traceback:", exc_info=True)
            self.logger.log_failure(pdf_path, f"Unexpected error: {e}")
            return 'failed'

    def _search(self, query: SearchQuery) -> List[CrossrefMatch]:
        """Search Crossref for the top five matches to `query`."""
        return self.crossref_client.search(
            title=query.title, author=query.author, year=query.year,
            max_results=5, author_reliable=query.author_reliable,
            prefer_preprint=query.preprint)

    def _add_datacite_theses(self, matches: List[CrossrefMatch],
                             query: SearchQuery) -> List[CrossrefMatch]:
        """Merge DataCite's dissertation matches into Crossref's, best first.

        DataCite is a second source here: if it fails, Crossref's results stand.
        """
        try:
            theses = self.crossref_client.search_datacite_theses(
                title=query.title, author=query.author, year=query.year)
        except (CrossrefConnectionError, CrossrefAPIError) as e:
            log.warning("DataCite dissertation search failed: %s", e)
            return matches
        found = {m.doi.lower() for m in matches}
        merged = matches + [m for m in theses if m.doi.lower() not in found]
        # Stable: on equal scores Crossref's own order (rank_key) stands.
        return sorted(merged, key=lambda m: m.score, reverse=True)[:5]

    def _match_to_metadata_update(self, match: CrossrefMatch) -> MetadataUpdate:
        """Convert CrossrefMatch to MetadataUpdate."""
        return MetadataUpdate(
            title=match.title,
            authors='; '.join(match.authors) if match.authors else '',
            year=match.year,
            journal=match.journal,
            doi=match.doi or None,   # a title-page record has none
            isbn=match.isbn
        )

    def _process_from_filename(self, pdf_path: str, filename_hints: FilenameHints) -> str:
        """
        Process a PDF using metadata extracted from its filename.

        This mode is used with --from-filename to set PDF metadata based on
        Zotero-style filenames without querying Crossref.

        Args:
            pdf_path: Path to the PDF file
            filename_hints: Parsed hints from the filename

        Returns:
            'completed', 'skipped', or 'failed'
        """
        # Check if filename parsing was successful
        if filename_hints.confidence < 0.5:
            if not self.ui.quiet:
                print(f"Skipped: filename doesn't match 'Author - Year - Title.pdf' "
                      f"(confidence: {filename_hints.confidence:.2f})")
            self.logger.log_skip(pdf_path, f"Filename format not recognized (confidence: {filename_hints.confidence:.2f})")
            return 'skipped'

        # Check for required fields
        missing_fields = []
        if not filename_hints.author:
            missing_fields.append('author')
        if not filename_hints.year:
            missing_fields.append('year')
        if not filename_hints.title:
            missing_fields.append('title')

        if missing_fields and not self.batch_mode:
            if not self.ui.quiet:
                print(f"Missing fields from filename: {', '.join(missing_fields)}")

        # Create metadata update from filename hints
        metadata_update = MetadataUpdate(
            title=filename_hints.title or '',
            authors=filename_hints.author or '',
            year=filename_hints.year,
            journal=None,  # Not available from filename
            doi=None       # Not available from filename
        )

        if not self.ui.quiet:
            print(f"\nMetadata from filename:")
            print(f"  Title:  {metadata_update.title or '(not found)'}")
            print(f"  Author: {metadata_update.authors or '(not found)'}")
            print(f"  Year:   {metadata_update.year or '(not found)'}")

        # In interactive mode, confirm before applying
        if not self.batch_mode:
            # Generate new filename if renaming is enabled
            if self.rename:
                new_filename = self.metadata_updater.generate_zotero_filename(
                    metadata_update,
                    pdf_path
                )
            else:
                new_filename = Path(pdf_path).name

            confirmed = self.ui.confirm_metadata(
                Path(pdf_path).name,
                metadata_update,
                new_filename
            )

            if not confirmed:
                self.logger.log_skip(pdf_path, "User declined metadata")
                return 'skipped'

        # Update PDF metadata
        if not self.ui.quiet:
            print("Updating PDF metadata...")

        try:
            self.metadata_updater.update_metadata(pdf_path, metadata_update)
        except (PDFUpdateError, FileOperationError) as e:
            log.error("%s: could not update metadata: %s", Path(pdf_path).name, e)
            self.logger.log_failure(pdf_path, str(e))
            return 'failed'

        # Rename file if enabled
        new_path = pdf_path
        if self.rename:
            new_filename = self.metadata_updater.generate_zotero_filename(
                metadata_update,
                pdf_path
            )
            if not self.ui.quiet:
                print(f"Renaming to: {new_filename}")

            try:
                new_path = self.metadata_updater.rename_file(
                    pdf_path,
                    new_filename
                )
            except (FileOperationError, FileNotFoundError) as e:
                log.error("%s: could not rename: %s", Path(pdf_path).name, e)
                self.logger.log_failure(pdf_path, str(e))
                return 'failed'

        # Log success
        self.logger.log_success(
            original_path=pdf_path,
            new_path=new_path,
            doi=None,  # No DOI in from-filename mode
            confidence=filename_hints.confidence,
            used_ocr=False
        )

        if not self.ui.quiet:
            print("Done")

        return 'completed'

    def process_files(self, files: List[str]) -> bool:
        """
        Process multiple PDF files.

        Args:
            files: List of PDF file paths

        Returns:
            True if processing was interrupted by the user, False otherwise.
        """
        total = len(files)
        interrupted = False

        if not self.ui.quiet:
            print(f"\nProcessing {total} PDF file{'s' if total != 1 else ''}...\n")

        try:
            for i, pdf_path in enumerate(files, 1):
                result = self.process_single_pdf(pdf_path)
                self.stats[result] += 1

                if not self.ui.quiet and total > 1:
                    self.ui.show_progress(
                        current=i,
                        total=total,
                        completed=self.stats['completed'],
                        skipped=self.stats['skipped'],
                        failed=self.stats['failed'],
                        current_file=Path(pdf_path).name
                    )

        except (KeyboardInterrupt, UserQuitError):
            interrupted = True
            if not self.ui.quiet:
                print("\n\nInterrupted")

        finally:
            self.logger.close()
            if not self.ui.quiet:
                self.ui.print_summary(
                    total=total,
                    completed=self.stats['completed'],
                    skipped=self.stats['skipped'],
                    failed=self.stats['failed'],
                    log_path=self.logger.log_path
                )

        return interrupted


def _expand_input(input_path: str, recursive: bool) -> List[str]:
    """Expand one file, directory, or glob pattern into PDF paths."""
    path = Path(input_path)
    if path.is_file():
        return [str(path)] if path.suffix.lower() == '.pdf' else []
    if path.is_dir():
        pattern = '**/*' if recursive else '*'
        return [str(p) for p in path.glob(pattern)
                if p.is_file() and p.suffix.lower() == '.pdf']
    # Quoted glob patterns reach us unexpanded; expand them ourselves.
    import glob
    return [p for p in glob.glob(input_path, recursive=recursive)
            if p.lower().endswith('.pdf')]


def collect_pdf_files(inputs: List[str], recursive: bool = False) -> List[str]:
    """
    Collect PDF files from one or more input paths.

    Args:
        inputs: Files, directories, or glob patterns
        recursive: Search directories recursively

    Returns:
        Sorted list of unique PDF file paths
    """
    files = set()
    for input_path in inputs:
        files.update(_expand_input(input_path, recursive))
    return sorted(files)


def parse_arguments(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list (default: sys.argv[1:])

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        prog='papertag',
        description='Update PDF metadata from academic sources',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s paper.pdf --email "me@example.com"
  %(prog)s papers/ --batch --backup --recursive
  %(prog)s *.pdf --no-rename --email "me@example.com"
  %(prog)s "Smith - 2020 - Title.pdf" --from-filename
  %(prog)s papers/ --from-filename --batch

Environment Variables:
  CROSSREF_EMAIL    Email for Crossref API (can be used instead of --email)
        '''
    )

    parser.add_argument(
        'inputs',
        nargs='+',
        metavar='PATH',
        help='PDF files, directories, or glob patterns to process'
    )

    parser.add_argument(
        '--version', '-V',
        action='version',
        version=f'%(prog)s {__version__}'
    )

    # Optional arguments
    parser.add_argument(
        '--email', '-e',
        help='Email for Crossref API (or set CROSSREF_EMAIL env var)'
    )

    parser.add_argument(
        '--batch', '-b',
        action='store_true',
        help=('Batch mode: apply the best match if it scores >= the threshold '
              'and clearly beats the runner-up; skip the file otherwise')
    )

    parser.add_argument(
        '--threshold', '-t',
        type=float,
        default=BATCH_THRESHOLD,
        metavar='SCORE',
        help=f'Minimum score for --batch to apply a search result (default: {BATCH_THRESHOLD:.2f})'
    )

    parser.add_argument(
        '--recursive', '-r',
        action='store_true',
        help='Search directories recursively'
    )

    parser.add_argument(
        '--no-rename',
        action='store_true',
        help='Do not rename files (only update metadata)'
    )

    parser.add_argument(
        '--from-filename',
        action='store_true',
        help='Set metadata from Zotero-style filename (Author - Year - Title.pdf). Skips Crossref lookup.'
    )

    parser.add_argument(
        '--backup',
        action='store_true',
        help='Keep .bak copy of original files'
    )

    parser.add_argument(
        '--no-ocr',
        action='store_true',
        help='Disable OCR for scanned documents'
    )

    parser.add_argument(
        '--ocr-pages',
        type=int,
        default=1,
        metavar='N',
        help='Number of pages to OCR (default: 1)'
    )

    parser.add_argument(
        '--retries',
        type=int,
        default=3,
        metavar='N',
        help='Number of retry attempts for Crossref API (default: 3)'
    )

    parser.add_argument(
        '--log',
        metavar='PATH',
        help='Write a JSON session log to PATH (default: no log file)'
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show detailed information'
    )

    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Minimal output (errors and summary only)'
    )

    return parser.parse_args(argv)


def validate_config(args: argparse.Namespace) -> Tuple[bool, Optional[str]]:
    """
    Validate configuration.

    Returns:
        Tuple of (valid, error_message)
    """
    # Check for email (not required for --from-filename mode)
    if not args.from_filename:
        if not args.email:
            args.email = os.environ.get('CROSSREF_EMAIL')

            if not args.email:
                return False, (
                    "Error: Email required for Crossref API.\n"
                    "Provide via --email or set CROSSREF_EMAIL environment variable.\n"
                    "Or use --from-filename to set metadata from the filename without Crossref."
                )

    # Check inputs exist (glob patterns are checked when expanded)
    for input_path in args.inputs:
        if not Path(input_path).exists() and not any(c in input_path for c in '*?['):
            return False, f"Error: Input path does not exist: {input_path}"

    # Validate numeric arguments
    if args.ocr_pages < 1:
        return False, "Error: --ocr-pages must be >= 1"

    if args.retries < 1:
        return False, "Error: --retries must be >= 1"

    if not 0 < args.threshold <= 1:
        return False, "Error: --threshold must be greater than 0 and at most 1"

    return True, None


def configure_logging(verbose: bool, quiet: bool) -> None:
    """Send papertag's diagnostics to stderr at a level set by the flags.

    Args:
        verbose: Show debug detail.
        quiet: Show errors only.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    logger = logging.getLogger('papertag')
    # Replace rather than add, so calling this twice doesn't double output.
    logger.handlers[:] = [handler]
    logger.propagate = False
    if verbose:
        logger.setLevel(logging.DEBUG)
    elif quiet:
        logger.setLevel(logging.ERROR)
    else:
        logger.setLevel(logging.INFO)

    # pypdf warns about every malformed structure it recovers from. We report
    # files we cannot read ourselves, so its chatter only helps when debugging.
    # pikepdf likewise logs a traceback when it recovers from a malformed XMP
    # packet; papertag then just ignores the XMP.
    for name in ('pypdf', 'pikepdf'):
        library_logger = logging.getLogger(name)
        library_logger.handlers[:] = [handler]
        library_logger.propagate = False
        library_logger.setLevel(logging.WARNING if verbose else logging.ERROR)


# Exit codes: 0 = no failures, 1 = a file failed or bad configuration,
# 130 = interrupted (the shell convention for SIGINT: 128 + 2).
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INTERRUPTED = 130


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point.

    Args:
        argv: Argument list (default: sys.argv[1:])

    Returns:
        Process exit code.
    """
    args = parse_arguments(argv)
    configure_logging(verbose=args.verbose, quiet=args.quiet)

    valid, error_msg = validate_config(args)
    if not valid:
        print(error_msg, file=sys.stderr)
        return EXIT_FAILURE

    files = collect_pdf_files(args.inputs, recursive=args.recursive)
    if not files:
        print(f"Error: No PDF files found in: {' '.join(args.inputs)}", file=sys.stderr)
        return EXIT_FAILURE

    manager = PDFMetadataManager(
        email=args.email,
        use_ocr=not args.no_ocr,
        ocr_pages=args.ocr_pages,
        keep_backup=args.backup,
        retries=args.retries,
        verbose=args.verbose,
        quiet=args.quiet,
        batch_mode=args.batch,
        rename=not args.no_rename,
        log_path=args.log,
        from_filename=args.from_filename,
        threshold=args.threshold,
    )

    interrupted = manager.process_files(files)

    if interrupted:
        return EXIT_INTERRUPTED
    if manager.stats['failed']:
        return EXIT_FAILURE
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
