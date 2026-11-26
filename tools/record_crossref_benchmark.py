#!/usr/bin/env python3
"""Record a Crossref search benchmark with known answers.

Samples random works from Crossref, builds the kinds of queries papertag sends
(from filename hints, from page text, title only, noisy OCR text, no title),
runs each through CrossrefClient.search exactly as papertag does, and saves the
raw candidates it scored. tests/test_scoring_benchmark.py replays the file
offline, so scoring changes can be measured without the network.

Uses only public Crossref records; sends no email or user data.

Usage:
    python tools/record_crossref_benchmark.py -n 60 -o tests/data/crossref_benchmark.json.gz

When papertag's query changes, re-issue the same queries (same works, so old
and new results compare) instead of sampling new works:
    python tools/record_crossref_benchmark.py --requery tests/data/crossref_benchmark.json.gz \
        -o tests/data/crossref_benchmark.json.gz
"""

import argparse
import gzip
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from papertag.core.crossref_client import CrossrefClient  # noqa: E402

USER_AGENT = "papertag-benchmark/1.0"
# Fields scoring and display need; the rest of a Crossref record is dropped to
# keep the fixture small.
KEEP = ["DOI", "title", "subtitle", "author", "published-print", "published-online",
        "issued", "created", "container-title", "type"]


class RecordingClient(CrossrefClient):
    """CrossrefClient that remembers the raw items of its last search."""

    def __init__(self) -> None:
        super().__init__(email="", retries=6, timeout=30, backoff_factor=2.0)
        self.headers = {"User-Agent": USER_AGENT}
        # The public pool (no mailto) has tighter limits than papertag's
        # polite-pool default.
        self.min_request_interval = 1.5
        self.last_items: List[Dict[str, Any]] = []
        self.last_url = ""

    def _make_request(self, url: str, method: str = "GET") -> Dict[str, Any]:
        data = super()._make_request(url, method)
        self.last_url = url
        self.last_items = data.get("message", {}).get("items", [])
        return data


# The fixture is committed, so keep its examples neutral: skip works whose
# title touches sensitive subjects.
SENSITIVE = re.compile(
    r"sex|gender|erot|porn|lgbt|gay|lesbian|transgender|religio|church|islam|muslim"
    r"|jew|christian|abortion|suicid|drug|alcohol|addict|hiv|aids\b|cancer|death|dying"
    r"|racis|ethnic|immigra|refugee|violen|abuse|assault|rape|war\b|terror|weapon"
    r"|politic|elect|crime|prison|psychiatr|depress|disorder|disease|patient|mental"
    r"|cervi[xc]|uter|vagin|pregnan|reproduct|breast|ethnicity|ancer\b",
    re.IGNORECASE)


def year_of(item: Dict[str, Any]) -> Optional[str]:
    """Publication year the way papertag reads it."""
    return CrossrefClient(email="")._extract_year(item)


def is_sensitive(item: Dict[str, Any]) -> bool:
    """True if the record's title or subtitle touches a sensitive subject."""
    return bool(SENSITIVE.search(" ".join((item.get("title") or []) + (item.get("subtitle") or []))))


def trim(item: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the fields scoring uses."""
    out = {k: item[k] for k in KEEP if k in item}
    if "author" in out:
        out["author"] = [{k: a[k] for k in ("given", "family", "name") if k in a}
                         for a in out["author"]]
    return out


def sample_works(n: int, seed: int) -> List[Dict[str, Any]]:
    """Random journal articles with a title, a named author and a year."""
    works: List[Dict[str, Any]] = []
    while len(works) < n:
        url = ("https://api.crossref.org/works?sample=100"
               "&filter=type:journal-article,from-pub-date:1970")
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
        resp.raise_for_status()
        for item in resp.json()["message"]["items"]:
            title = (item.get("title") or [""])[0]
            authors = [a for a in item.get("author", []) if a.get("family")]
            year = year_of(item)
            # Short or non-Latin titles are not what papertag mostly meets.
            words = re.findall(r"[A-Za-z]{2,}", title)
            if len(words) >= 4 and authors and year and len(title) < 250 and not is_sensitive(item):
                works.append(item)
        time.sleep(1)
    random.Random(seed).shuffle(works)
    return works[:n]


def full_title(work: Dict[str, Any]) -> str:
    """Title as printed on the page: with its subtitle."""
    title = work["title"][0]
    subtitle = (work.get("subtitle") or [""])[0]
    return f"{title}: {subtitle}" if subtitle and subtitle.lower() not in title.lower() else title


def noisy(title: str, rng: random.Random) -> str:
    """Imitate page-text damage: caps, a line-break hyphen, a dropped letter."""
    words = title.split()
    long_words = [i for i, w in enumerate(words) if len(w) >= 7]
    if long_words:
        i = rng.choice(long_words)
        words[i] = words[i][:3] + "- " + words[i][3:]
    if len(long_words) > 1:
        j = rng.choice([k for k in long_words if k != i])
        words[j] = words[j][:4] + words[j][5:]
    return " ".join(words).upper()


def queries_for(work: Dict[str, Any], rng: random.Random) -> List[Dict[str, Optional[str]]]:
    """The query shapes papertag builds, from best to worst evidence."""
    authors = [a for a in work["author"] if a.get("family")]
    first = authors[0]["family"]
    year = year_of(work)
    title = full_title(work)
    author_line = ", ".join(f"{a.get('given', '')} {a['family']}".strip() for a in authors[:4])
    first_line = " ".join(title.split()[:8])
    shapes = [
        # Zotero-style filename: first author, year, title.
        {"variant": "filename", "title": title, "author": first, "year": year},
        # Page text: first line of a wrapped title, the author line, no year.
        {"variant": "text", "title": first_line, "author": author_line, "year": None},
        {"variant": "title_only", "title": title, "author": None, "year": None},
        {"variant": "noisy", "title": noisy(title, rng), "author": first, "year": year},
        # Filename with author and year but no title.
        {"variant": "no_title", "title": None, "author": first, "year": year},
    ]
    for shape in shapes:
        shape["author_reliable"] = author_reliable(shape["variant"])
    return shapes


def author_reliable(variant: str) -> bool:
    """papertag sends a page-text author as a guess (see cli.SearchQuery)."""
    return variant != "text"


QUERY_KEYS = ("title", "author", "year", "author_reliable")


def record_case(client: RecordingClient, case_id: str, variant: str, true_doi: str,
                query: Dict[str, Any]) -> Dict[str, Any]:
    """Run one query as papertag would and keep what scoring needs."""
    client.search(title=query["title"], author=query["author"], year=query["year"],
                  author_reliable=query["author_reliable"])
    return {
        "id": case_id,
        "variant": variant,
        "true_doi": true_doi,
        "query": {k: query[k] for k in QUERY_KEYS},
        "url": client.last_url,
        # Candidates are dropped too: the fixture is public.
        "items": [trim(i) for i in client.last_items if not is_sensitive(i)],
    }


def requery(path: Path) -> Dict[str, Any]:
    """Re-issue every query of an existing fixture with papertag's current search."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        fixture = json.load(f)
    client = RecordingClient()
    cases = []
    for n, old in enumerate(fixture["cases"], 1):
        query = {**old["query"]}
        query.setdefault("author_reliable", author_reliable(old["variant"]))
        cases.append(record_case(client, old["id"], old["variant"], old["true_doi"], query))
        print(f"{n}/{len(fixture['cases'])}", file=sys.stderr)
    return {"seed": fixture.get("seed"), "cases": cases}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-n", "--works", type=int, default=60, help="works to sample")
    parser.add_argument("-s", "--seed", type=int, default=1)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--requery", type=Path, metavar="FIXTURE",
                        help="re-issue this fixture's queries instead of sampling new works")
    args = parser.parse_args(argv)

    if args.requery:
        write(requery(args.requery), args.output)
        return 0

    rng = random.Random(args.seed)
    works = sample_works(args.works, args.seed)
    client = RecordingClient()
    cases = []
    for n, work in enumerate(works, 1):
        for q in queries_for(work, rng):
            cases.append(record_case(client, f"{work['DOI']}#{q['variant']}", q["variant"],
                                     work["DOI"].lower(), q))
        print(f"{n}/{len(works)} {work['DOI']}", file=sys.stderr)

    write({"seed": args.seed, "cases": cases}, args.output)
    return 0


def write(fixture: Dict[str, Any], output: Path) -> None:
    """Save a fixture as compact gzipped JSON."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output, "wt", encoding="utf-8") as f:
        json.dump(fixture, f, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {len(fixture['cases'])} cases to {output}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
