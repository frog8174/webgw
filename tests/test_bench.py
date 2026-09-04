"""Run the benchmark cases as tests, so the fixtures cannot rot unnoticed.

bench/run.py exists to be read by a human -- it prints ranked lists and scores.
This wires the same cases into pytest so CI fails on a retrieval regression
without anyone remembering to run the bench by hand.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

BENCH = pathlib.Path(__file__).resolve().parents[1] / "bench"
sys.path.insert(0, str(BENCH))

from run import BUDGET, run_case  # noqa: E402


def _cases() -> list[dict]:
    with open(BENCH / "cases.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["id"])
def test_bench_case(case: dict) -> None:
    ok, why = run_case(case, verbose=False)
    assert ok, f"{case['id']}: {why}\n  {case.get('note', '')}"


def test_budget_matches_shipped_default() -> None:
    """The bench must measure what production uses, or its numbers mean nothing."""
    from webgw.config import Config

    assert BUDGET == Config().select_budget_tokens
