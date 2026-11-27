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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from papertag import __version__

log = logging.getLogger(__name__)


# Custom exceptions
class CrossrefConnectionError(Exception):
    """Raised when connection to Crossref API fails after all retries."""
    pass


class CrossrefAPIError(Exception):
    """Raised for non-retryable API errors (e.g., 404, malformed requests).

    Also raised for DataCite, papertag's second DOI registry.
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


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
    work_type: Optional[str] = None  # Crossref `type`, e.g. journal-article, posted-content

    @property
    def is_preprint(self) -> bool:
        """Whether Crossref files this record as a preprint (posted-content)."""
        return self.work_type == 'posted-content'

    @property
    def confidence_level(self) -> str:
        """Return HIGH/MEDIUM/LOW based on score."""
        if self.score >= BATCH_THRESHOLD:
            return "HIGH"
        elif self.score >= 0.65:
            return "MEDIUM"
        else:
            return "LOW"


# Batch mode accepts the top match only at this score, and only if it leads
# the next rival by the margin. On the recorded benchmark the margin removed
# both wrong accepts (two records of one study, scored equal) at a cost of two
# right ones; 0.10 would cost seven more.
BATCH_THRESHOLD = 0.80
BATCH_MARGIN = 0.05


@dataclass
class BatchDecision:
    """Batch mode's verdict on a ranked candidate list."""
    match: Optional[CrossrefMatch]  # None: skip the file
    reason: str = ''                # why it was skipped


def batch_choice(matches: List[CrossrefMatch], threshold: float = BATCH_THRESHOLD,
                 margin: float = BATCH_MARGIN) -> BatchDecision:
    """Decide whether batch mode may apply the top match without asking.

    The top match must score at least `threshold` and lead the best rival by
    at least `margin`. A rival is any other candidate except the article (or
    preprint) counterpart of a preprint (or article) pick: those are usually
    the same work, and rank_key has already chosen the form the PDF is.

    Args:
        matches: Candidates, best first (as CrossrefClient.search returns them).
        threshold: Minimum score to accept.
        margin: Minimum lead over the best rival.

    Returns:
        The match to apply, or None with the reason for skipping.
    """
    if not matches:
        return BatchDecision(None, 'No Crossref matches found')
    top = matches[0]
    if top.score < threshold:
        return BatchDecision(
            None, f'Confidence below threshold: {top.score:.2f} < {threshold:.2f}')
    rival = next((m for m in matches[1:] if m.is_preprint == top.is_preprint), None)
    # Round so that a lead of exactly the margin isn't lost to float error.
    if rival and round(top.score - rival.score, 6) < margin:
        return BatchDecision(
            None, f'Ambiguous: {top.doi} ({top.score:.2f}) vs {rival.doi} ({rival.score:.2f})')
    return BatchDecision(top)


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

    def _make_request(self, url: str, method: str = 'GET',
                      headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        Make HTTP request with retry logic and exponential backoff.

        Args:
            url: The URL to request
            method: HTTP method (default: GET)
            headers: Headers to send instead of the Crossref ones

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
                    headers=headers or self.headers,
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
                        f"API error {response.status_code}: {response.text}",
                        status_code=response.status_code,
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

    # A record read off page 1 by its DOI must have this share of its title's
    # words (3+ letters, no stopwords) on the page. On ~210 page DOIs that
    # resolved in a 400-PDF sample, all but one right record had >= 0.8; the
    # exception was a page whose text layer runs words together.
    PAGE_TITLE_MIN_SHARE = 0.6
    # Below this many characters of page text there is nothing to check against.
    MIN_PAGE_TEXT = 100
    # The supplement's record repeats the paper's title, so its words are on
    # the page; Crossref files it as a journal-article without authors.
    SUPPLEMENT_TITLE = re.compile(
        r'^\s*(?:supplement(?:al|ary)?|supporting)\s+(?:materials?|information|data|info)\b',
        re.IGNORECASE)
    # Some text layers drop the spaces between words ("GlacialValleysof..."),
    # and Chinese or Japanese titles have none. A title word this long also
    # counts if it is in the page text with all spaces removed.
    MIN_RUN_TOGETHER_WORD = 5

    def shown_on_page(self, match: CrossrefMatch, page_text: str) -> bool:
        """True if a record looked up by a page-1 DOI is the paper on that page.

        A DOI on page 1 can be a cited work's or the supplement's. The paper's
        own record has its title on the page; another work's mostly doesn't.

        Args:
            match: The record fetched for the DOI.
            page_text: Text of page 1 (OCR text for a scanned page).

        Returns:
            False for a supplement record, a record without title words to
            check, or a title mostly missing from the page; True otherwise,
            including when the page has too little text to tell.
        """
        if self.SUPPLEMENT_TITLE.match(self._plain_text(match.title or '')):
            return False
        if len(page_text.strip()) < self.MIN_PAGE_TEXT:
            return True
        # A hyphen at a line end splits a word ("High-\nlands") or ends the
        # first half of a compound ("glacier-\nfed"); count both readings.
        joined = re.sub(r'-\s*\n\s*', '', page_text)
        page_words = set(self._plain_words(page_text)) | set(self._plain_words(joined))
        squashed = ''.join(self._plain_words(joined))
        title_words = [w for w in self._title_words(match.title or '') if len(w) > 2]
        if not title_words:
            # Component and dataset records: nothing to confirm them by.
            return False
        shown = sum(w in page_words
                    or (len(w) >= self.MIN_RUN_TOGETHER_WORD and w in squashed)
                    for w in title_words)
        return shown / len(title_words) >= self.PAGE_TITLE_MIN_SHARE

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
            similarity = self._item_title_similarity(title, item)
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

    def _item_title_similarity(self, title: str, item: Dict[str, Any]) -> float:
        """Similarity of `title` to a record's title, with or without its subtitle.

        Crossref files many subtitles apart from the title; a page or
        filename title may have it or not.
        """
        candidates = [t for t in (item.get('title') or []) if t]
        subtitle = next((s for s in (item.get('subtitle') or []) if s), None)
        if candidates and subtitle:
            candidates.append(f"{candidates[0]}: {subtitle}")
        return max((self._fuzzy_title_similarity(title, c) for c in candidates), default=0.0)

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

    @staticmethod
    def first_author_surname(author: str) -> Optional[str]:
        """The first author's family name in a filename or page author string.

        Handles "Moreno et al.", "Moreno and Park", "Moreno, Ana",
        "Ana Moreno1, Lee Park*" and Vancouver-style "Moreno A, Park L".

        Args:
            author: Author text as papertag found it.

        Returns:
            The surname, or None if the text holds no name.
        """
        text = re.sub(r'\bet\s+al\b\.?', ' ', author)
        first = re.split(r'\s+(?:and|&)\s+|[;·]', text)[0]
        first = re.sub(r'[\d*†‡§¶]+', ' ', first)   # affiliation markers
        parts = [p for p in (part.strip() for part in first.split(',')) if p]
        if not parts:
            return None
        # Initials ("A.", Vancouver "Moreno A") are never the surname.
        words = [w for w in re.findall(r"[^\W\d_][\w'’-]*", parts[0]) if len(w) > 1]
        if not words:
            return None
        # "Moreno, Ana": one word before the comma is the surname. "Ana Moreno,
        # Lee Park": the first name in a list ends with the surname.
        if len(parts) > 1 and len(words) == 1:
            return words[0]
        return words[-1]

    # Fields scoring and display read; the rest of a record is not downloaded.
    SEARCH_SELECT = ("DOI,title,subtitle,author,issued,published-print,published-online,"
                     "created,container-title,type")

    @classmethod
    def search_url(cls, title: Optional[str], author: Optional[str], year: Optional[str],
                   rows: int, author_reliable: bool = True) -> Optional[str]:
        """Build the Crossref search request for papertag's evidence.

        The title and year go to query.bibliographic, which Crossref ranks
        like a citation; the first author's surname goes to query.author,
        which matches contributor names only. On the recorded benchmark this
        found the right work among the candidates more often than one plain
        query, most of all for weak evidence (author and year, no title).

        Args:
            title: Title text, if any. All-caps titles are sent in title case.
            author: Author text as found (filename, page line); only the first
                author's surname is sent.
            year: Publication year, if any.
            rows: Number of results to ask for.
            author_reliable: False for a guess (page-text author line). Its
                surname then joins query.bibliographic instead: query.author
                nearly filters, and a wrong "surname" ("University") pushed
                the right paper out of the results.

        Returns:
            The request URL, or None when there is nothing to search with.
        """
        if title and title.isupper():
            title = title.title()
        surname = cls.first_author_surname(author) if author else None
        field_author = surname if author_reliable else None
        loose_author = None if author_reliable else surname
        bibliographic = " ".join(part for part in (title, loose_author, year) if part)
        params = {}
        if bibliographic:
            params["query.bibliographic"] = bibliographic
        if field_author:
            params["query.author"] = field_author
        if not params:
            return None
        params.update(rows=rows, select=cls.SEARCH_SELECT)
        return f"https://api.crossref.org/works?{urlencode(params)}"

    @staticmethod
    def rank_key(score: float, item: Dict[str, Any],
                 prefer_preprint: bool = False) -> Tuple[float, bool]:
        """Sort key for a scored Crossref item, highest first.

        On a tie between a published record and a preprint (posted-content)
        of the same work, the one matching the PDF wins: the article for a
        publisher PDF, the preprint for a PDF with a preprint server's stamp.
        Crossref's order used to decide, and batch mode picked a preprint
        for a PDF carrying the article's DOI.
        """
        is_preprint = item.get('type') == 'posted-content'
        return score, is_preprint == prefer_preprint

    def search(
        self,
        title: Optional[str] = None,
        author: Optional[str] = None,
        year: Optional[str] = None,
        max_results: int = 5,
        author_reliable: bool = True,
        prefer_preprint: bool = False,
    ) -> List[CrossrefMatch]:
        """
        Search Crossref for matching publications.

        Args:
            title: Article title (most important)
            author: Author name or hint
            year: Publication year
            max_results: Maximum number of results to return
            author_reliable: False when the author is a guess (see search_url)
            prefer_preprint: The PDF is a preprint; on a score tie, prefer a
                preprint record over the published article (see rank_key)

        Returns:
            List of CrossrefMatch objects sorted by score (highest first)

        Raises:
            CrossrefConnectionError: After all retries exhausted
            CrossrefAPIError: For non-retryable API errors
        """
        url = self.search_url(title, author, year, rows=max_results * 2,
                              author_reliable=author_reliable)
        if url is None:
            return []

        try:
            data = self._make_request(url)
        except (CrossrefConnectionError, CrossrefAPIError):
            raise

        # Extract and score results
        ranked = []
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
                    score=score,
                    work_type=item.get('type'),
                )

                ranked.append((self.rank_key(score, item, prefer_preprint), match))

        ranked.sort(key=lambda entry: entry[0], reverse=True)
        return [match for _, match in ranked[:max_results]]

    # Prefixes registered only with DataCite; asking Crossref first would be
    # a wasted request. arXiv registers one for every paper.
    DATACITE_PREFIXES = ("10.48550/",)
    # DataCite resourceTypeGeneral -> Crossref `type`, so is_preprint and the
    # article/preprint rules work the same for both registries.
    DATACITE_TYPES = {
        "Preprint": "posted-content", "JournalArticle": "journal-article",
        "Dissertation": "dissertation", "Book": "book", "BookChapter": "book-chapter",
        "Report": "report", "Dataset": "dataset",
    }

    def fetch_metadata(self, doi: str) -> CrossrefMatch:
        """
        Fetch complete metadata for a given DOI.

        DOIs Crossref doesn't know (404) are looked up in DataCite, which
        registers arXiv's, Zenodo's and many repositories' DOIs.

        Args:
            doi: The DOI to look up (bare or as a doi.org URL)

        Returns:
            CrossrefMatch with score 1.0, since an exact DOI lookup needs
            no fuzzy matching.

        Raises:
            CrossrefConnectionError: After all retries exhausted
            CrossrefAPIError: For non-retryable API errors (404 from both
                registries, etc.) or a response without a record
        """
        cleaned_doi = doi.strip()
        if cleaned_doi.startswith('http'):
            doi_match = re.search(r'(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)', cleaned_doi)
            if doi_match:
                cleaned_doi = doi_match.group(1)

        if cleaned_doi.lower().startswith(self.DATACITE_PREFIXES):
            return self.fetch_datacite(cleaned_doi)
        try:
            data = self._make_request(f"https://api.crossref.org/works/{cleaned_doi}")
        except CrossrefAPIError as e:
            if e.status_code != 404:
                raise
            log.debug("DOI %s is not in Crossref; trying DataCite", cleaned_doi)
            return self.fetch_datacite(cleaned_doi)

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
            work_type=item.get('type'),
        )

    def fetch_datacite(self, doi: str) -> CrossrefMatch:
        """Fetch a DOI's record from DataCite.

        Sent without the email: that is for Crossref's polite pool.

        Args:
            doi: A bare DOI.

        Returns:
            CrossrefMatch with score 1.0.

        Raises:
            CrossrefConnectionError: After all retries exhausted
            CrossrefAPIError: DataCite doesn't have the DOI either, or sent
                no record
        """
        url = f"https://api.datacite.org/dois/{doi}"
        data = self._make_request(url, headers={'User-Agent': f'papertag/{__version__}'})
        attributes = (data.get('data') or {}).get('attributes')
        if not attributes:
            raise CrossrefAPIError(f"DataCite returned no record for DOI {doi}")
        match = self._datacite_match(attributes)
        match.doi = match.doi or doi
        # Some arXiv records say "Text", but every arXiv DOI is a preprint.
        if doi.lower().startswith("10.48550/arxiv."):
            match.work_type = "posted-content"
        return match

    def _datacite_match(self, attributes: Dict[str, Any]) -> CrossrefMatch:
        """Convert a DataCite record's attributes to a CrossrefMatch (score 1.0)."""
        titles = attributes.get('titles') or []
        main = [t for t in titles if not t.get('titleType')] or titles
        authors = []
        for creator in attributes.get('creators') or []:
            given, family = creator.get('givenName'), creator.get('familyName')
            if given and family:
                authors.append(f"{given} {family}")
            elif family or creator.get('name'):
                authors.append(family or creator['name'])
        # Newer API versions can send the publisher as an object.
        publisher = attributes.get('publisher')
        if isinstance(publisher, dict):
            publisher = publisher.get('name')
        container = (attributes.get('container') or {}).get('title')
        year = attributes.get('publicationYear')
        resource_type = (attributes.get('types') or {}).get('resourceTypeGeneral')
        return CrossrefMatch(
            doi=attributes.get('doi') or '',
            title=main[0].get('title', '') if main else '',
            authors=authors,
            year=str(year) if year else None,
            journal=container or publisher or None,
            score=1.0,
            work_type=self.DATACITE_TYPES.get(resource_type,
                                              resource_type.lower() if resource_type else None),
        )

    # Crossref types for a whole book, which loses a tie to a chapter.
    BOOK_TYPES = {"book", "monograph", "edited-book", "reference-book", "book-set"}

    # Records one book's ISBN can return: the book and its chapters, ranked
    # by the page title. Large edited volumes have dozens of chapters.
    ISBN_ROWS = 20

    @classmethod
    def isbn_url(cls, isbn: str, title: str, rows: int = ISBN_ROWS) -> str:
        """Crossref request for a book's records, ranked by `title`."""
        params = {"filter": f"isbn:{isbn}", "query.bibliographic": title,
                  "rows": rows, "select": cls.SEARCH_SELECT + ",ISBN"}
        return f"https://api.crossref.org/works?{urlencode(params)}"

    def isbn_record(self, isbn: str, title: Optional[str],
                    page_text: str) -> Optional[CrossrefMatch]:
        """The record, among one book's, that is the PDF: the book or a chapter.

        A chapter PDF carries its book's ISBN, so the ISBN alone can't say
        which record the file is. The one whose title is the page's title
        (similarity >= MIN_TITLE_FOR_HIGH, and on the page) is; among equals
        a chapter beats the book, whose title a chapter page often shows too.

        Args:
            isbn: ISBN-13 found in the PDF.
            title: The page's title; without one there is nothing to choose by.
            page_text: Text of page 1.

        Returns:
            The record with score 1.0 and `isbn` set, or None.

        Raises:
            CrossrefConnectionError: After all retries exhausted
            CrossrefAPIError: For non-retryable API errors
        """
        if not title:
            return None
        data = self._make_request(self.isbn_url(isbn, title))
        best, best_key = None, None
        for item in (data.get('message') or {}).get('items') or []:
            if not item.get('DOI') or not item.get('title'):
                continue
            similarity = self._item_title_similarity(title, item)
            if similarity < self.MIN_TITLE_FOR_HIGH:
                continue
            match = CrossrefMatch(
                doi=item['DOI'], title=item['title'][0], authors=self._extract_authors(item),
                year=self._extract_year(item),
                journal=(item.get('container-title') or [None])[0],
                score=1.0, isbn=isbn, work_type=item.get('type'))
            if not self.shown_on_page(match, page_text):
                continue
            key = (similarity, item.get('type') not in self.BOOK_TYPES)
            if best_key is None or key > best_key:
                best, best_key = match, key
        return best
