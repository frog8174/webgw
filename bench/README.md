# Retrieval benchmark

Hermetic. No network, no crawl4ai, no deployment — it runs `sections.split()` and
`ranking.rank()` on committed fixtures, which is the same code path the gateway
uses to decide what you get back.

```bash
uv run python bench/run.py       # summary, non-zero exit on regression
uv run python bench/run.py -v    # ranked list and scores per case
uv run --extra dev pytest -q     # the same cases, wired into the suite
```

## Why this exists

The rank@1 figures in the top-level README (21/30 → 24/30 → 28/30) were measured
during development against a case set that was never committed. They cannot be
reproduced from this repository, and that is a real gap — see *Not implemented*
there.

This directory is the beginning of closing it. It does not reproduce those
numbers. It is a small set of cases with a known-correct answer, chosen because
each one pins down a specific behaviour that was, or could be, got wrong.

## The cases

| id | What it pins down |
|---|---|
| `attn-mechanism` | **A regression.** A 36-token section whose only body is an `[edit]` link ranked first against a live deployment on 2026-09-04 |
| `attn-scaled-dot` | Literal term matching, where BM25 should be strong |
| `tokenizer` | An unambiguous single-section answer |
| `why-parallel` | Paraphrase — the section never says "faster". This is what reranking is for |
| `chrome-immunity` | A navigation-only query ranks navigation first, not content |

## The regression these were written for

`how does multi-head attention work` against the Transformer article returned:

```
1. Subsequent work            36 tok   body is one [edit] link
2. Transformer (deep learning) 2,800 tok  35 interlanguage links
3. FlashAttention
4. Parallelizing attention
5. Tokenization
```

45% of the returned budget was chrome, while `Attention head` (3,828 tok) and
`Multihead attention` (1,847 tok) — the sections that answer the question — were
pushed out entirely. `match.confidence` said `medium`.

The cause was a disagreement inside the pipeline. `MIN_SECTION_TOKENS` was applied
to the raw body, but `terms()` strips links before scoring. A section containing
nothing but a long URL cleared a raw-token threshold while carrying no prose.
Admission now measures the same link-stripped text that scoring does.

## Adding a case

Append to `cases.jsonl`:

```jsonc
{
  "id": "short-slug",
  "fixture": "transformer.md",
  "query": "what a user would actually type",
  "expect": ["Section title that should rank first"],  // any one of these
  "reject": ["Section that must not be selected"],     // optional
  "note": "Why this case exists. If it encodes a bug, say when it was observed."
}
```

Fixtures are markdown as crawl4ai produces it — links, `[edit]` decorations and
navigation left in, because those are exactly what the ranking has to cope with.
A cleaned-up fixture would test something the gateway never sees.
