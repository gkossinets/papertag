"""
Crossref API client with retry logic, fuzzy matching, and improved scoring.

This module provides a robust client for searching and fetching metadata from
the Crossref API, with exponential backoff retry logic, rate limiting, and
sophisticated matching algorithms.
"""

import html
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from urllib.parse import quote_plus

import requests

from papertag import __version__

log = logging.getLogger(__name__)


# Custom exceptions
class CrossrefConnectionError(Exception):
    """Raised when connection to Crossref API fails after all retries."""
    pass


class CrossrefAPIError(Exception):
    """Raised for non-retryable API errors (e.g., 404, malformed requests)."""
    pass


@dataclass
class CrossrefMatch:
    """A potential match from Crossref."""
    doi: str
    title: str
    authors: List[str]
    year: Optional[str]
    journal: Optional[str]
    score: float  # 0.0 to 1.0
    isbn: Optional[str] = None

    @property
    def confidence_level(self) -> str:
        """Return HIGH/MEDIUM/LOW based on score."""
        if self.score >= 0.80:
            return "HIGH"
        elif self.score >= 0.65:
            return "MEDIUM"
        else:
            return "LOW"


class CrossrefClient:
    """Client for Crossref API with retry logic and rate limiting."""

    # Common words to ignore in title matching
    STOPWORDS = {
        'the', 'a', 'an', 'of', 'in', 'on', 'at', 'to', 'for', 'with',
        'from', 'by', 'and', 'or', 'but', 'is', 'are', 'was', 'were',
        'this', 'that', 'these', 'those', 'have', 'has', 'had', 'there'
    }

    def __init__(
        self,
        email: str,
        retries: int = 3,
        timeout: int = 30,
        backoff_factor: float = 1.0
    ):
        """
        Initialize Crossref client.

        Args:
            email: Your email (for polite pool and contact)
            retries: Number of retry attempts for failed requests
            timeout: Request timeout in seconds
            backoff_factor: Base delay for exponential backoff (seconds)
        """
        self.email = email
        self.retries = retries
        self.timeout = timeout
        self.backoff_factor = backoff_factor
        self.last_request_time = 0
        self.min_request_interval = 0.5  # Rate limiting: 0.5s between requests

        # User-Agent for polite pool
        self.headers = {
            'User-Agent': f'papertag/{__version__} (mailto:{email})'
        }

    def _wait_for_rate_limit(self):
        """Ensure we respect rate limiting between requests."""
        time_since_last = time.time() - self.last_request_time
        if time_since_last < self.min_request_interval:
            time.sleep(self.min_request_interval - time_since_last)
        self.last_request_time = time.time()

    # Don't let a server-sent Retry-After stall a batch run indefinitely.
    MAX_RETRY_AFTER = 60

    @classmethod
    def _retry_after(cls, response: Any) -> Optional[float]:
        """Seconds a 429 response asks us to wait, if it says (numeric form only)."""
        value = (getattr(response, 'headers', None) or {}).get('Retry-After')
        try:
            return min(float(value), cls.MAX_RETRY_AFTER) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _make_request(self, url: str, method: str = 'GET') -> Dict[str, Any]:
        """
        Make HTTP request with retry logic and exponential backoff.

        Args:
            url: The URL to request
            method: HTTP method (default: GET)

        Returns:
            Response JSON as dictionary

        Raises:
            CrossrefConnectionError: After all retries exhausted
            CrossrefAPIError: For non-retryable API errors
        """
        last_exception = None

        for attempt in range(self.retries):
            try:
                self._wait_for_rate_limit()

                response = requests.request(
                    method,
                    url,
                    headers=self.headers,
                    timeout=self.timeout
                )

                # Handle different HTTP status codes
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    # Throttled: the request was fine, so wait and retry rather
                    # than failing the file like other 4xx errors.
                    last_exception = requests.exceptions.RequestException(
                        "Rate limited by Crossref (HTTP 429)")
                    if attempt < self.retries - 1:
                        delay = self._retry_after(response) or self.backoff_factor * (2 ** attempt)
                        log.info("Crossref rate limit hit; waiting %.0f s", delay)
                        time.sleep(delay)
                        continue
                    raise CrossrefConnectionError(
                        f"Still rate limited after {self.retries} attempts"
                    ) from last_exception
                elif 400 <= response.status_code < 500:
                    # Client error - don't retry
                    raise CrossrefAPIError(
                        f"API error {response.status_code}: {response.text}"
                    )
                elif response.status_code >= 500:
                    # Server error - retry
                    raise requests.exceptions.RequestException(
                        f"Server error {response.status_code}"
                    )

            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.RequestException) as e:
                last_exception = e

                if attempt < self.retries - 1:
                    # Exponential backoff: 1s, 2s, 4s
                    delay = self.backoff_factor * (2 ** attempt)
                    time.sleep(delay)
                    continue
                else:
                    # All retries exhausted
                    raise CrossrefConnectionError(
                        f"Connection failed after {self.retries} attempts: {str(e)}"
                    ) from e

        # Should not reach here, but just in case
        raise CrossrefConnectionError(
            f"Connection failed: {str(last_exception)}"
        ) from last_exception

    # Relative weight of each kind of evidence. The score is normalized by the
    # weights of the evidence the query actually has, so a query without an
    # author can still reach 1.0 on title and year.
    TITLE_WEIGHT = 0.5
    AUTHOR_WEIGHT = 0.2
    YEAR_WEIGHT = 0.2
    # A title alone can't tell a paper from others with a similar or generic
    # title, and author + year alone match everything that author published
    # that year. Keep both below the batch auto-accept threshold (0.80).
    TITLE_ONLY_CAP = 0.75
    NO_TITLE_CAP = 0.5
    # Author and year can't make up for a title that differs: same-author
    # papers with similar titles otherwise pass the batch threshold. In the
    # benchmark (tests/data/crossref_benchmark.json) right answers had title
    # similarity >= 0.87 and such wrong ones 0.64-0.83.
    MIN_TITLE_FOR_HIGH = 0.85
    WEAK_TITLE_CAP = 0.75
    # Text-layer damage tolerated in words this long; a damaged word counts
    # this much of an exact one.
    MIN_FUZZY_WORD_LENGTH = 5
    FUZZY_WORD_CREDIT = 0.9
    # A query this many words long that matches the start of a candidate title
    # is treated as that title's first line (page text cuts wrapped titles).
    MIN_PREFIX_WORDS = 4
    PREFIX_DISCOUNT = 0.9

    @staticmethod
    def _plain_text(text: str) -> str:
        """Lowercase text without markup, accents or ligatures."""
        # Crossref titles carry inline HTML ("TiO<sub>2</sub>", "<i>Quercus</i>").
        # Drop tags without a space so "TiO<sub>2</sub>" stays one word.
        text = html.unescape(re.sub(r'<[^>]+>', '', text))
        text = unicodedata.normalize('NFKD', unicodedata.normalize('NFKC', text))
        text = ''.join(c for c in text if not unicodedata.combining(c)).casefold()
        # Rejoin a word split by a line-break hyphen ("rech- arge").
        return re.sub(r'(\w)- +(\w)', r'\1\2', text)

    def _plain_words(self, text: str) -> List[str]:
        """Words of `text` (underscores separate words, as in filenames)."""
        return re.findall(r'[^\W_]+', self._plain_text(text))

    def _title_words(self, title: str) -> List[str]:
        """Content words of a title, in order."""
        return [w for w in self._plain_words(title) if w not in self.STOPWORDS]

    @staticmethod
    def _edit_distance_at_most_one(a: str, b: str) -> bool:
        """True if one substitution, insertion, deletion or adjacent swap turns a into b."""
        if abs(len(a) - len(b)) > 1:
            return False
        if len(a) == len(b):
            diffs = [i for i in range(len(a)) if a[i] != b[i]]
            if len(diffs) == 1:
                return True
            return (len(diffs) == 2 and diffs[1] == diffs[0] + 1
                    and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]])
        shorter, longer = sorted((a, b), key=len)
        return any(longer[:i] + longer[i + 1:] == shorter for i in range(len(longer)))

    def _words_match(self, a: str, b: str) -> bool:
        """Same word, allowing one slip of page-text damage in a longer word.

        Deliberately narrow: words that differ by a prefix are often opposites
        (supervised/unsupervised, linear/nonlinear, hyper-/hypotension), so the
        first two letters must agree and only one character may differ.
        """
        if a == b:
            return True
        if min(len(a), len(b)) < self.MIN_FUZZY_WORD_LENGTH:
            return False
        if not (a.isalpha() and b.isalpha()) or a[:2] != b[:2]:
            return False
        return self._edit_distance_at_most_one(a, b)

    def _fuzzy_title_similarity(self, title1: str, title2: str) -> float:
        """Similarity of two titles by shared content words, 0.0 to 1.0.

        Word-based (Dice coefficient), so unrelated titles that share a word
        or two score low, which character-sequence matching did not do. A
        damaged word counts slightly less than an exact one, so the exact
        title wins a tie. A query that matches the beginning of a longer title
        (the first line of a wrapped title) scores below a full match.

        Args:
            title1: Query title.
            title2: Candidate title.

        Returns:
            Similarity between 0.0 and 1.0.
        """
        query = self._title_words(title1)
        candidate = self._title_words(title2)
        if not query or not candidate:
            return 0.0

        # Exact pairs first, so a fuzzy pairing can't take an exact word's partner.
        unmatched_query = list(query)
        unused = list(candidate)
        matched = 0.0
        for word in list(unmatched_query):
            if word in unused:
                unused.remove(word)
                unmatched_query.remove(word)
                matched += 1
        for word in unmatched_query:
            for i, other in enumerate(unused):
                if self._words_match(word, other):
                    del unused[i]
                    matched += self.FUZZY_WORD_CREDIT
                    break
        dice = 2 * matched / (len(query) + len(candidate))

        prefix = 0.0
        if len(query) >= self.MIN_PREFIX_WORDS and len(query) < len(candidate):
            if all(self._words_match(q, c) for q, c in zip(query, candidate)):
                prefix = self.PREFIX_DISCOUNT * matched / len(query)
        return max(dice, prefix)

    # Particles that don't identify a person on their own ("van der Berg").
    NAME_PARTICLES = {'van', 'von', 'der', 'den', 'de', 'del', 'della', 'di', 'da',
                      'dos', 'du', 'la', 'le', 'ter', 'ten', 'af', 'al', 'el', 'bin', 'ibn'}

    def _name_words(self, text: str) -> List[str]:
        """Letters-only words: drops affiliation markers ("Smith1", "Smith¹", "Smith*")."""
        return re.findall(r'[^\W\d_]+', self._plain_text(text.replace('\u2019', "'")))

    def _author_matches(self, query_author: str, item: Dict[str, Any]) -> bool:
        """True if some author's family name appears in the query as whole words.

        Whole words, so "Li" does not match "Lipton". Particles are ignored
        ("Berg" matches "van der Berg"), and one part of a hyphenated name is
        enough ("Smith" matches "Smith-Jones"). Given names are ignored.
        """
        query_words = set(self._name_words(query_author))
        for author in item.get('author') or []:
            family = author.get('family')
            if not family:
                continue
            core = [w for w in self._name_words(family) if w not in self.NAME_PARTICLES]
            if core and all(w in query_words for w in core):
                return True
            if '-' in family:
                parts = [self._name_words(p) for p in family.split('-')]
                if any(p and all(w in query_words for w in p) for p in parts):
                    return True
        return False

    def _year_similarity(self, query_year: str, item: Dict[str, Any]) -> float:
        """1.0 for the same year, 0.5 for an adjacent one (online vs print), else 0."""
        item_year = self._extract_year(item)
        try:
            gap = abs(int(item_year) - int(query_year))
        except (TypeError, ValueError):
            return 0.0
        return {0: 1.0, 1: 0.5}.get(gap, 0.0)

    def _calculate_match_score(
        self,
        item: Dict[str, Any],
        title: Optional[str] = None,
        author: Optional[str] = None,
        year: Optional[str] = None
    ) -> float:
        """Score how well a Crossref item matches the query, 0.0 to 1.0.

        A weighted average over the evidence the query has (title, author,
        year); evidence the query lacks neither adds nor subtracts. Evidence
        the query has but the item lacks counts as a mismatch. Title-only and
        title-less queries, and titles that aren't a near match, are capped
        below the batch threshold.

        Args:
            item: Crossref API result item
            title: Query title
            author: Query author(s), any format
            year: Query year

        Returns:
            Score between 0.0 and 1.0
        """
        earned = 0.0
        possible = 0.0

        similarity = 0.0
        if title:
            candidates = [t for t in (item.get('title') or []) if t]
            subtitle = next((s for s in (item.get('subtitle') or []) if s), None)
            if candidates and subtitle:
                candidates.append(f"{candidates[0]}: {subtitle}")
            similarity = max((self._fuzzy_title_similarity(title, c) for c in candidates),
                             default=0.0)
            earned += self.TITLE_WEIGHT * similarity
            possible += self.TITLE_WEIGHT

        if author:
            earned += self.AUTHOR_WEIGHT * self._author_matches(author, item)
            possible += self.AUTHOR_WEIGHT

        if year:
            earned += self.YEAR_WEIGHT * self._year_similarity(year, item)
            possible += self.YEAR_WEIGHT

        if not possible:
            return 0.0
        score = earned / possible
        if not title:
            score = min(score, self.NO_TITLE_CAP)
        elif not author and not year:
            score = min(score, self.TITLE_ONLY_CAP)
        elif similarity < self.MIN_TITLE_FOR_HIGH:
            score = min(score, self.WEAK_TITLE_CAP)
        return min(score, 1.0)

    def _extract_year(self, item: Dict[str, Any]) -> Optional[str]:
        """Extract publication year from Crossref item."""
        # Try different date fields in order of preference
        # 'issued' is the standard publication date in Crossref and covers
        # records that lack 'published-print'/'published-online' (e.g. books,
        # conference papers, older articles). 'created' is the Crossref
        # registration date and is only used as a last resort.
        for date_field in ['published-print', 'published-online', 'issued', 'created']:
            if date_field in item and 'date-parts' in item[date_field]:
                date_parts = item[date_field]['date-parts']
                if date_parts and len(date_parts) > 0 and len(date_parts[0]) > 0:
                    year_val = date_parts[0][0]
                    # Some records have None inside date-parts (e.g. [[None]])
                    if year_val is not None:
                        try:
                            return str(int(year_val))
                        except (ValueError, TypeError):
                            continue
        return None

    def _extract_authors(self, item: Dict[str, Any]) -> List[str]:
        """Extract author names from Crossref item."""
        authors = []
        if 'author' in item:
            for author in item['author']:
                if 'given' in author and 'family' in author:
                    authors.append(f"{author['given']} {author['family']}")
                elif 'family' in author:
                    authors.append(author['family'])
        return authors

    def search(
        self,
        title: Optional[str] = None,
        author: Optional[str] = None,
        year: Optional[str] = None,
        max_results: int = 5
    ) -> List[CrossrefMatch]:
        """
        Search Crossref for matching publications.

        Args:
            title: Article title (most important)
            author: Author name or hint
            year: Publication year
            max_results: Maximum number of results to return

        Returns:
            List of CrossrefMatch objects sorted by score (highest first)

        Raises:
            CrossrefConnectionError: After all retries exhausted
            CrossrefAPIError: For non-retryable API errors
        """
        # Build query string
        query_parts = []

        if title:
            # Handle all-caps titles
            if title.isupper():
                title = title.title()
            query_parts.append(title)

        if author:
            # Extract last name if possible
            lastname_match = re.search(r'([A-Z][a-zA-Z\-]+),', author)
            if lastname_match:
                query_parts.append(lastname_match.group(1))
            else:
                # Try to extract last name from "First Last" format
                words = author.split()
                if len(words) > 1:
                    query_parts.append(words[-1])
                else:
                    query_parts.append(author)

        if year:
            query_parts.append(year)

        if not query_parts:
            return []

        # Limit query to most important parts
        query = " ".join(query_parts[:3])
        encoded_query = quote_plus(query)

        # Make API request
        url = f"https://api.crossref.org/works?query={encoded_query}&rows={max_results * 2}"

        try:
            data = self._make_request(url)
        except (CrossrefConnectionError, CrossrefAPIError):
            raise

        # Extract and score results
        matches = []
        if 'message' in data and 'items' in data['message']:
            items = data['message']['items']

            for item in items:
                # Must have DOI and title
                if 'DOI' not in item or 'title' not in item or not item['title']:
                    continue

                # Calculate match score
                score = self._calculate_match_score(item, title, author, year)

                # Create match object
                match = CrossrefMatch(
                    doi=item['DOI'],
                    title=item['title'][0],
                    authors=self._extract_authors(item),
                    year=self._extract_year(item),
                    journal=(item.get('container-title') or [None])[0],
                    score=score
                )

                matches.append(match)

        # Sort by score (highest first) and limit results
        matches.sort(key=lambda m: m.score, reverse=True)
        return matches[:max_results]

    def fetch_metadata(self, doi: str) -> CrossrefMatch:
        """
        Fetch complete metadata for a given DOI.

        Args:
            doi: The DOI to look up (bare or as a doi.org URL)

        Returns:
            CrossrefMatch with score 1.0, since an exact DOI lookup needs
            no fuzzy matching.

        Raises:
            CrossrefConnectionError: After all retries exhausted
            CrossrefAPIError: For non-retryable API errors (404, etc.) or a
                response without a 'message' record
        """
        cleaned_doi = doi.strip()
        if cleaned_doi.startswith('http'):
            doi_match = re.search(r'(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)', cleaned_doi)
            if doi_match:
                cleaned_doi = doi_match.group(1)

        data = self._make_request(f"https://api.crossref.org/works/{cleaned_doi}")

        item = data.get('message')
        if not item:
            raise CrossrefAPIError(f"Crossref returned no record for DOI {cleaned_doi}")

        isbn = item.get('ISBN')
        if isinstance(isbn, list):
            isbn = isbn[0] if isbn else None

        return CrossrefMatch(
            doi=item.get('DOI') or cleaned_doi,
            title=(item.get('title') or [''])[0],
            authors=self._extract_authors(item),
            year=self._extract_year(item),
            journal=(item.get('container-title') or [None])[0],
            score=1.0,
            isbn=isbn,
        )
