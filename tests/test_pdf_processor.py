"""
Unit tests for PDF Processor

Tests the PDFProcessor class with various scenarios.
"""

import os
import sys
import unittest
from unittest.mock import Mock, patch, MagicMock

from papertag.core.pdf_processor import (
    PDFProcessor,
    PDFMetadata,
    PDFNotFoundError,
    PDFReadError,
    OCRNotAvailableError
)


class TestPDFProcessor(unittest.TestCase):
    """Test cases for PDFProcessor class."""

    def setUp(self):
        """Set up test fixtures."""
        self.processor = PDFProcessor(use_ocr=False, verbose=False)

    def test_initialization(self):
        """Test PDFProcessor initialization."""
        processor = PDFProcessor(use_ocr=True, ocr_pages=2, verbose=True)
        self.assertEqual(processor.ocr_pages, 2)
        self.assertTrue(processor.verbose)

    def test_sanitize_doi(self):
        """Test DOI sanitization."""
        # Test trailing period removal
        doi = self.processor._sanitize_doi("10.1234/test...")
        self.assertEqual(doi, "10.1234/test")

        # Test whitespace removal
        doi = self.processor._sanitize_doi("  10.1234/test  ")
        self.assertEqual(doi, "10.1234/test")

    def test_extract_doi_from_text(self):
        """Test DOI extraction from text."""
        # Test standard DOI format
        text = "This paper DOI: 10.1234/test.5678 is interesting"
        doi = self.processor._extract_doi(text)
        self.assertEqual(doi, "10.1234/test.5678")

        # Test DOI with URL
        text = "Available at https://doi.org/10.1234/test.5678"
        doi = self.processor._extract_doi(text)
        self.assertEqual(doi, "10.1234/test.5678")

        # Test bare DOI
        text = "The DOI is 10.1234/test.5678 for this work"
        doi = self.processor._extract_doi(text)
        self.assertEqual(doi, "10.1234/test.5678")

        # Test no DOI
        text = "This text has no DOI"
        doi = self.processor._extract_doi(text)
        self.assertIsNone(doi)

    def test_pdf_not_found(self):
        """Test handling of non-existent PDF files."""
        with self.assertRaises(PDFNotFoundError):
            self.processor.extract_metadata("/nonexistent/file.pdf")

    def test_extract_title_from_lines(self):
        """Test title extraction from filtered lines."""
        # Test with all-caps title
        lines = [
            "Some header",
            "MACHINE LEARNING FOR CLIMATE SCIENCE",
            "Some other text",
            "More content"
        ]
        title = self.processor._extract_title(lines)
        self.assertEqual(title, "MACHINE LEARNING FOR CLIMATE SCIENCE")

        # Test with no valid title
        lines = ["short", "abc", "12"]
        title = self.processor._extract_title(lines)
        self.assertIsNone(title)

    def test_extract_journal(self):
        """Test journal name extraction."""
        lines = [
            "Some text",
            "Journal of Climate Science, Vol. 10",
            "More text"
        ]
        journal = self.processor._extract_journal(lines)
        self.assertIsNotNone(journal)
        self.assertIn("Journal of Climate Science", journal)

        # Test with no journal
        lines = ["No journal here", "Just text"]
        journal = self.processor._extract_journal(lines)
        self.assertIsNone(journal)


# --- Text extraction against real PDFs -------------------------------------

import pypdf
import pytest

from papertag.core import pdf_processor
from tests.pdf_fixtures import make_text_pdf


def test_text_extraction_uses_pypdf():
    """PyPDF2 is deprecated; extraction must go through its successor."""
    assert pdf_processor.PdfReader is pypdf.PdfReader


def test_extracts_first_page_text(tmp_path):
    path = make_text_pdf(tmp_path / "paper.pdf",
                         ["Deep Learning for Everyone", "Jane Doe", "doi:10.1234/abc.5678"])
    text = PDFProcessor(use_ocr=False)._extract_text(str(path))
    assert "Deep Learning for Everyone" in text
    assert "10.1234/abc.5678" in text


def test_unreadable_pdf_raises_read_error(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"this is not a pdf")
    with pytest.raises(PDFReadError):
        PDFProcessor(use_ocr=False)._extract_text(str(path))
