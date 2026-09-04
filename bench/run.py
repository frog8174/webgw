# -*- coding: utf-8 -*-
"""Hermetic retrieval benchmark: rank@1 over committed fixtures.

No network, no crawl4ai, no deployment. It exercises the same code path the
gateway uses -- sections.split() then ranking.rank() -- so a change in ranking
shows up here before it reaches anyone.

    uv run python bench/run.py            # summary
    uv run python bench/run.py -v         # per-case ranked list

Exit code is non-zero when any case regresses, so this can gate a commit.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from webgw import sections as S  # noqa: E402
from webgw.ranking import rank, select  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
BUDGET = 8000


def load_cases() -> list[dict]:
    with open(HERE / "cases.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def run_case(case: dict, verbose: bool) -> tuple[bool, str]:
    md = (HERE / "fixtures" / case["fixture"]).read_text(encoding="utf-8")
    secs = S.split(md)
    r = rank(secs, case["query"])
    sel = select(secs, r, BUDGET)

    ordered = [secs[i].title for i in r.order]
    picked = [p.section.title for p in sel.picks]
    top = ordered[0] if ordered else "(none)"

    expect, reject = case.get("expect") or [], case.get("reject") or []

    problems = []
    if expect and top not in expect:
        problems.append(f"rank@1 is {top!r}, wanted one of {expect}")
    for bad in reject:
        if bad in picked:
            problems.append(f"{bad!r} was selected into the budget")
    if expect and not any(e in picked for e in expect):
        problems.append(f"none of {expect} fit in the {BUDGET}-token budget")

    if verbose:
        print(f"\n  {case['id']}  query={case['query']!r}")
        print(f"    confidence={r.stats.confidence} top_score={r.stats.top_score:.2f}")
        for n, i in enumerate(r.order[:6], 1):
            mark = "*" if secs[i].title in picked else " "
            print(f"    {mark}{n}. {secs[i].title:<28} "
                  f"score={r.scores[i]:6.2f}  {secs[i].tokens:>5} tok")

    return not problems, "; ".join(problems)


def main() -> int:
    verbose = "-v" in sys.argv
    cases = load_cases()
    failed = []
    for c in cases:
        ok, why = run_case(c, verbose)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {c['id']:<18} {why}")
        if not ok:
            failed.append(c["id"])

    print(f"\n  {len(cases) - len(failed)}/{len(cases)} passing")
    if failed:
        print(f"  regressed: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
