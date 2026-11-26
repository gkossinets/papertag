"""Replay the recorded Crossref benchmark against papertag's match scoring.

tests/data/crossref_benchmark.json.gz holds real Crossref search results for
queries whose right answer is known (see tools/record_crossref_benchmark.py).
Replaying it offline measures what batch mode would do, which is what matters:
it writes metadata and renames the file whenever batch_choice accepts the top
match (score at least the threshold, and a margin over the runner-up).

Run ``python -m tests.scoring_benchmark`` for a report.
"""

import gzip
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

from papertag.core.crossref_client import (BATCH_MARGIN, BATCH_THRESHOLD, CrossrefClient,
                                           CrossrefMatch, batch_choice)

BENCHMARK = Path(__file__).parent / "data" / "crossref_benchmark.json.gz"

ScoreFn = Callable[[Dict[str, Any], Dict[str, Any]], float]


@dataclass
class VariantStats:
    """Outcomes for one query variant.

    "Present" cases score the candidates Crossref returned. "Absent" cases
    score the same candidates with the right work removed, standing in for a
    paper Crossref doesn't have: any accept there is a wrong file rename.
    """
    cases: int = 0
    found: int = 0               # right work among Crossref's candidates
    top1: int = 0                # ... and ranked first
    accept_right: int = 0        # batch would accept, and it's the right work
    accept_wrong: int = 0        # batch would accept the wrong work
    absent_cases: int = 0
    absent_accept: int = 0       # accepted something though the work is absent
    wrong_examples: List[str] = field(default_factory=list)


def load_cases(path: Path = BENCHMARK) -> List[Dict[str, Any]]:
    """Load benchmark cases."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)["cases"]


def with_derived_variants(cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add query shapes papertag really sends that the recording lacks.

    Each derived case reuses its source case's candidates, so it measures
    scoring, not what Crossref would return for that query string:
    - filename_us: the title as parse_filename leaves it for
      Author_Year_Title.pdf, with underscores for spaces.
    - text_marked: the page-text author line with affiliation markers.
    """
    derived = []
    for case in cases:
        query = case["query"]
        if case["variant"] == "filename":
            derived.append({**case, "id": case["id"] + "_us", "variant": "filename_us",
                            "query": {**query, "title": query["title"].replace(" ", "_")}})
        elif case["variant"] == "text" and query["author"]:
            names = [n.strip() for n in query["author"].split(",")]
            marked = ", ".join(f"{n}{i + 1}" if i % 2 == 0 else f"{n}*" for i, n in enumerate(names))
            derived.append({**case, "id": case["id"] + "_marked", "variant": "text_marked",
                            "query": {**query, "author": marked}})
    return cases + derived


def _ranked(items: List[Dict[str, Any]], query: Dict[str, Any],
            score: ScoreFn) -> List[CrossrefMatch]:
    """Score and rank items as CrossrefClient.search does, tie-break included."""
    scored = []
    for item in items:
        s = score(item, query)
        match = CrossrefMatch(doi=item["DOI"].lower(), title=(item.get("title") or [""])[0],
                              authors=[], year=None, journal=None, score=s,
                              work_type=item.get("type"))
        scored.append((CrossrefClient.rank_key(s, item), match))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [match for _, match in scored]


def evaluate(cases: List[Dict[str, Any]], score: ScoreFn,
             threshold: float = BATCH_THRESHOLD,
             margin: float = BATCH_MARGIN) -> Dict[str, VariantStats]:
    """Score every case and tally batch-mode outcomes by query variant.

    Args:
        cases: Benchmark cases.
        score: ``score(item, query)`` for one Crossref item and a query dict
            with title/author/year.
        threshold: Batch auto-accept threshold.
        margin: Batch lead required over the runner-up.

    Returns:
        Stats keyed by variant name, plus "all".
    """
    stats: Dict[str, VariantStats] = defaultdict(VariantStats)
    for case in cases:
        items = case["items"]
        if not items:
            continue
        ranked = _ranked(items, case["query"], score)
        truth = case["true_doi"]
        present = any(m.doi == truth for m in ranked)
        accepted = batch_choice(ranked, threshold, margin).match
        # The same candidates without the right work: stands in for a paper
        # Crossref doesn't have.
        absent = [m for m in ranked if m.doi != truth]
        absent_accepted = batch_choice(absent, threshold, margin).match
        for key in (case["variant"], "all"):
            s = stats[key]
            s.cases += 1
            s.found += present
            if ranked[0].doi == truth:
                s.top1 += 1
            if accepted:
                if accepted.doi == truth:
                    s.accept_right += 1
                else:
                    s.accept_wrong += 1
                    if key != "all":
                        s.wrong_examples.append(
                            f"{case['id']} -> {accepted.doi} ({accepted.score:.2f})")
            if present and absent:
                s.absent_cases += 1
                s.absent_accept += absent_accepted is not None
    return dict(stats)


def report(stats: Dict[str, VariantStats]) -> str:
    """Format stats as a table."""
    rows = [f"{'variant':<11} {'cases':>5} {'found':>5} {'top1':>5} "
            f"{'acc.ok':>6} {'acc.bad':>7} {'absent.acc':>10}"]
    for name in sorted(stats, key=lambda n: (n == "all", n)):
        s = stats[name]
        rows.append(f"{name:<11} {s.cases:>5} {s.found:>5} {s.top1:>5} "
                    f"{s.accept_right:>6} {s.accept_wrong:>7} "
                    f"{s.absent_accept:>4}/{s.absent_cases:<5}")
    for name in sorted(stats):
        for example in stats[name].wrong_examples:
            rows.append(f"  wrong accept: {example}")
    return "\n".join(rows)


def papertag_score() -> ScoreFn:
    """papertag's current scoring as a ScoreFn."""
    client = CrossrefClient(email="benchmark@example.invalid")
    return lambda item, q: client._calculate_match_score(
        item, title=q["title"], author=q["author"], year=q["year"])


if __name__ == "__main__":
    print(report(evaluate(with_derived_variants(load_cases()), papertag_score())))
