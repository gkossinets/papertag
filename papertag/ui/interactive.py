"""Interactive user interface for PDF processing.

This module provides user-friendly interactive prompts for match selection,
error handling, and confirmations during PDF metadata processing.
"""

import shutil
import sys
import textwrap
from typing import List, Optional

from ..core.crossref_client import CrossrefMatch
from ..core.filename_parser import FilenameHints
from ..core.metadata_updater import MetadataUpdate


class UserQuitError(Exception):
    """Raised when user chooses to quit."""
    pass


class InteractiveUI:
    """Interactive user interface for PDF processing."""

    def __init__(self, verbose: bool = False, quiet: bool = False):
        """
        Initialize UI.

        Args:
            verbose: Show detailed information
            quiet: Minimal output (errors and summary only)
        """
        self.verbose = verbose
        self.quiet = quiet

    def display_matches(
        self,
        matches: List[CrossrefMatch],
        filename: str,
        filename_hints: FilenameHints
    ) -> Optional[CrossrefMatch]:
        """
        Display matches and get user selection.

        Args:
            matches: List of potential matches
            filename: Original PDF filename
            filename_hints: Parsed filename information

        Returns:
            - Selected CrossrefMatch if user picks a numbered option
            - None if user skips
            - 'retry' if user wants to retry search
            - ('manual', doi) if user enters a manual DOI
            - 'from_filename' if user chooses to use filename metadata

        Raises:
            UserQuitError: If user chooses to quit
        """
        if self.quiet:
            return None

        # Display header
        print(f"\nFound {len(matches)} potential match{'es' if len(matches) != 1 else ''} for: {filename}")
        print()

        # Display filename hints if available
        hint_parts = []
        if filename_hints.author:
            hint_parts.append(f"Author: {filename_hints.author}")
        if filename_hints.year:
            hint_parts.append(f"Year: {filename_hints.year}")
        if filename_hints.title:
            hint_parts.append(f"Title: {filename_hints.title}")

        if hint_parts:
            print("Current filename info: " + ", ".join(hint_parts))
            print()

        # Display matches
        for i, match in enumerate(matches, 1):
            # Confidence indicator
            if match.confidence_level == "HIGH":
                stars = "★★★"
            elif match.confidence_level == "MEDIUM":
                stars = "★★"
            else:
                stars = "★"

            print(f"{i}. {stars} {match.confidence_level} CONFIDENCE ({match.score:.2f})")
            print(f"   Title: {match.title}")

            # Format authors
            if match.authors:
                if len(match.authors) <= 3:
                    authors_str = "; ".join(match.authors)
                else:
                    authors_str = "; ".join(match.authors[:3]) + f"; ... ({len(match.authors)} total)"
                print(f"   Authors: {authors_str}")

            if match.year:
                print(f"   Year: {match.year}")
            if match.journal:
                print(f"   Journal: {match.journal}")
            print(f"   DOI: {match.doi or '(none; read from the title page)'}")
            print()

        # Show filename-based metadata option if we have usable hints
        has_filename_option = (
            filename_hints.confidence >= 0.5 and
            (filename_hints.author or filename_hints.title)
        )

        if has_filename_option:
            print("Metadata from filename (no DOI):")
            print(f"   Title:   {filename_hints.title or '(not found)'}")
            print(f"   Author:  {filename_hints.author or '(not found)'}")
            print(f"   Year:    {filename_hints.year or '(not found)'}")
            print()

        # Get user choice
        while True:
            try:
                if has_filename_option:
                    prompt = f"Choose: [1-{len(matches)}] Select match | [s]kip | [r]etry | [m]anual DOI | [f]rom filename | [q]uit\n> "
                else:
                    prompt = f"Choose: [1-{len(matches)}] Select match | [s]kip | [r]etry | [m]anual DOI | [q]uit\n> "
                choice = input(prompt).strip().lower()

                if choice == 'q':
                    raise UserQuitError("User chose to quit")
                elif choice == 's':
                    return None
                elif choice == 'r':
                    # Return a special marker for retry
                    return 'retry'  # type: ignore
                elif choice == 'm':
                    # Get manual DOI
                    doi = input("Enter DOI: ").strip()
                    if doi:
                        # Return a marker for manual DOI entry
                        return ('manual', doi)  # type: ignore
                    else:
                        print("No DOI entered, skipping...")
                        return None
                elif choice == 'f':
                    if has_filename_option:
                        # Return a marker for using filename metadata
                        return 'from_filename'  # type: ignore
                    else:
                        print("Filename doesn't contain enough metadata. Use a different option.")
                else:
                    # Try to parse as number
                    try:
                        idx = int(choice)
                        if 1 <= idx <= len(matches):
                            return matches[idx - 1]
                        else:
                            print(f"Please enter a number between 1 and {len(matches)}")
                    except ValueError:
                        print(f"Invalid choice: {choice}")

            except EOFError:
                # Handle Ctrl+D
                print("\nEOF detected, skipping...")
                return None
            except KeyboardInterrupt:
                # Handle Ctrl+C
                print("\nInterrupted, skipping...")
                return None

    def show_changes(
        self,
        current: MetadataUpdate,
        new: MetadataUpdate,
        filename: str,
        new_filename: str,
    ) -> None:
        """Print current and new metadata side by side, changed rows marked "*".

        Long values wrap within their column rather than being cut: the title
        is what the user most needs to check.

        Args:
            current: What the PDF has now (as MetadataUpdater.read_metadata reads it).
            new: What would be written.
            filename: The file's current name.
            new_filename: The name it would get.
        """
        if self.quiet:
            return
        rows = [("Title", current.title, new.title),
                ("Authors", current.authors, new.authors),
                ("Year", current.year, new.year),
                ("Journal", current.journal, new.journal),
                ("DOI", current.doi, new.doi)]
        if current.isbn or new.isbn:
            rows.append(("ISBN", current.isbn, new.isbn))
        rows.append(("Filename", filename, new_filename))

        label_width = 10
        width = min(shutil.get_terminal_size((100, 24)).columns, self.MAX_TABLE_WIDTH)
        column = max((width - 2 - label_width - 2) // 2, 10)

        def cell(value: Optional[str]) -> List[str]:
            return textwrap.wrap(str(value), column) if value else ["(none)"]

        print()
        print(f"  {'':<{label_width}}{'Current':<{column}}  New")
        for label, old, updated in rows:
            changed = (old or '') != (updated or '')
            left, right = cell(old), cell(updated)
            for i in range(max(len(left), len(right))):
                marker = "* " if changed and i == 0 else "  "
                name = label if i == 0 else ''
                old_part = left[i] if i < len(left) else ''
                new_part = right[i] if i < len(right) else ''
                print(f"{marker}{name:<{label_width}}{old_part:<{column}}  {new_part}".rstrip())
        print()

    # Wider tables are hard to read across.
    MAX_TABLE_WIDTH = 140

    def confirm_metadata(
        self,
        filename: str,
        metadata: MetadataUpdate,
        new_filename: str,
        current: Optional[MetadataUpdate] = None,
    ) -> bool:
        """
        Show the change and confirm with user.

        Args:
            filename: Original filename
            metadata: Metadata to write
            new_filename: Proposed new filename
            current: The PDF's metadata now (default: shown as empty)

        Returns:
            True if user approves, False if skip

        Raises:
            UserQuitError: If user chooses to quit
        """
        if self.quiet:
            return True

        print("\nReview metadata update:")
        self.show_changes(current or MetadataUpdate(title='', authors=''),
                          metadata, filename, new_filename)

        # Get confirmation
        while True:
            try:
                choice = input("[a]pply | [s]kip | [q]uit\n> ").strip().lower()

                if choice == 'q':
                    raise UserQuitError("User chose to quit")
                elif choice == 's':
                    return False
                elif choice == 'a':
                    return True
                else:
                    print(f"Invalid choice: {choice}")

            except EOFError:
                print("\nEOF detected, skipping...")
                return False
            except KeyboardInterrupt:
                print("\nInterrupted, skipping...")
                return False

    def handle_error(
        self,
        filename: str,
        error: Exception,
        retryable: bool = True
    ) -> str:
        """
        Display error and get user choice.

        Args:
            filename: File that caused error
            error: The exception
            retryable: Whether retry is an option

        Returns:
            User choice: 'retry', 'skip', or 'quit'
        """
        print(f"\nError processing: {filename}")
        print(f"Error: {str(error)}")
        print()

        if retryable:
            prompt = "[r]etry | [s]kip | [q]uit\n> "
            valid_choices = ['r', 's', 'q']
        else:
            prompt = "[s]kip | [q]uit\n> "
            valid_choices = ['s', 'q']

        while True:
            try:
                choice = input(prompt).strip().lower()

                if choice == 'q':
                    return 'quit'
                elif choice == 's':
                    return 'skip'
                elif choice == 'r' and retryable:
                    return 'retry'
                else:
                    print(f"Invalid choice: {choice}")

            except EOFError:
                print("\nEOF detected, skipping...")
                return 'skip'
            except KeyboardInterrupt:
                print("\nInterrupted, quitting...")
                return 'quit'

    def show_progress(
        self,
        current: int,
        total: int,
        completed: int,
        skipped: int,
        failed: int,
        current_file: str
    ):
        """Print one progress line after a file.

        Not redrawn in place: each file's own output comes between progress
        lines, and moving the cursor up to redraw erased it. One plain line
        also reads well when stdout is piped to a file.

        Args:
            current: Current file number (1-indexed)
            total: Total files to process
            completed: Number of successfully completed files
            skipped: Number of skipped files
            failed: Number of failed files
            current_file: Name of the file just processed (already shown in
                its header, so not repeated)
        """
        if self.quiet:
            return
        print(f"\nProgress: {current}/{total}  completed {completed}, "
              f"skipped {skipped}, failed {failed}")

    def print_summary(
        self,
        total: int,
        completed: int,
        skipped: int,
        failed: int,
        log_path: str,
        dry_run: bool = False,
    ):
        """
        Print final summary.

        Args:
            total: Total files processed
            completed: Number successfully completed (with dry_run: would be)
            skipped: Number skipped
            failed: Number failed
            log_path: Path to log file
            dry_run: Nothing was written; say so
        """
        print("\n" + "=" * 70)
        print("PROCESSING SUMMARY (DRY RUN)" if dry_run else "PROCESSING SUMMARY")
        print("=" * 70)
        print()
        print(f"Total files:      {total}")
        if dry_run:
            print(f"Would update:     {completed}")
        else:
            print(f"Completed:        {completed}")
        print(f"Skipped:          {skipped}")
        print(f"Failed:           {failed}")
        print()

        if completed > 0:
            success_rate = (completed / total) * 100
            print(f"Success rate:     {success_rate:.1f}%")
            print()

        if log_path:
            print(f"Log file:         {log_path}")
            print()

        if dry_run:
            print("Dry run: nothing was written or renamed.")
            print()

        print("=" * 70)

    def info(self, message: str):
        """Print informational message."""
        if not self.quiet:
            print(message)

    def verbose_info(self, message: str):
        """Print verbose informational message."""
        if self.verbose and not self.quiet:
            print(f"[VERBOSE] {message}")

    def error(self, message: str):
        """Print error message (always shown)."""
        print(f"ERROR: {message}", file=sys.stderr)

    def warning(self, message: str):
        """Print warning message."""
        if not self.quiet:
            print(f"WARNING: {message}")
