# webgw

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker image](https://img.shields.io/badge/docker-abc99012%2Fwebgw%3A0.3.1-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/abc99012/webgw)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-2025--11--25-6E56CF)](https://modelcontextprotocol.io)

A query-aware web fetch gateway for local LLM agents, built on [crawl4ai](https://github.com/unclecode/crawl4ai).

Exposes a single MCP tool, `web_fetch(url, query)`, over streamable HTTP. Given a
page and a query, it returns the **verbatim passages** that match, rather than a
summary — so a model with an 8k context can read a 90k-token page.

```
web_fetch(url="https://example.com/release-notes", query="breaking changes in 2.0")
  -> excerpts from the 3 relevant sections, ~8k tokens
     plus the outline of what was left out, and how much it would cost
```

## Use it if

- You run **local models in an agent** (OpenCode, or any MCP client) and have no
  built-in web fetch. Hosted assistants mostly ship one already.
- The pages you read are **much larger than your context** — docs, wikis, long
  release notes. Below is where the saving actually lands.
- You need **verbatim source text**, not a summary, because you have to quote it
  or check it.

## Don't use it if

- **The page is only slightly bigger than your budget.** A 9k-token page against
  an 8k budget returns 7.7k — nearly everything, navigation chrome included.
  There is nothing to cut.
- **You want a summary.** This returns passages. Summarising is the caller's job,
  on content it can verify.
- **The site blocks crawlers.** Anti-bot walls are reported honestly
  (`blocked_antibot`) rather than worked around.

## Contents

[Why](#why) · [What it actually does](#what-it-actually-does) ·
[Quick start](#quick-start) · [Response format](#response-format) ·
[Two deployment decisions](#two-decisions-that-shape-your-deployment) ·
[Retrieval modes](#retrieval-modes) · [Outcomes](#outcomes) ·
[Connecting a client](#connecting-a-client) · [Kubernetes](#kubernetes) ·
[Configuration](#configuration) · [Caching](#caching) · [Security](#security) ·
[Design notes](#design-notes) · [Troubleshooting](#troubleshooting) ·
[Versions](#versions)

## Why

Local models typically have 8k–32k of context. The median web page is
**14,451 tokens** of raw markdown — one page eats half the budget — and the
largest measured was 135,294, which does not fit at all.[^1]

[^1]: Median over the 30 pages in this project's own test set (GitHub releases,
    Wikipedia, arXiv listings, OpenReview, Chinese tech news), counted with
    `cl100k_base` after crawl4ai's markdown conversion. The 135,294-token page
    was an OpenReview forum thread. This is a sample from one person's browsing,
    not a claim about the web at large — see the note on reproducibility below.

Summarising loses information and cannot be verified. webgw instead selects
verbatim sections by relevance and reports what it left out, so the agent can
tell whether the answer might be elsewhere and ask again.

| Configuration | rank@1 | Added cost |
|---|---|---|
| BM25 | 21/30 | — |
| BM25 + Traditional/Simplified normalization | **24/30** | +145 ms |
| plus cross-encoder reranking | **28/30** | +3,000 ms |

*30 ground-truth cases: 12 English, 18 Chinese. rank@1 counts how often the
correct section ranked first.*

> **On reproducibility.** Two separate case sets are quoted in this README: the
> 30-case set above, and a 17-case Chinese-only set under [Design
> notes](#design-notes). They are different experiments, not the same numbers
> reported twice — do not try to reconcile 18 with 17.
>
> Neither harness is in this repository yet. The figures are what was measured
> during development, but you cannot currently re-run them here, so treat them as
> the author's measurements rather than as independently verifiable results.
> Committing the harness is the top item under [Not
> implemented](#not-implemented).

## What it actually does

Real calls against a deployed instance, 2026-09-04. Every number below is
measured, not illustrative.

| Case | `outcome` | raw → returned | `mode` | confidence | time |
|---|---|---|---|---|---|
| Small page, no query | `ok` | 33 → 33 | `passthrough` | — | — |
| Large page + query | `ok` | 89,847 → **6,353** | `bm25` | medium | 0.7 s |
| Same page, `mode="rerank"` | `ok` | 89,847 → **7,000** | `rerank` | **high** | 8.3 s |
| Traditional query, Simplified page | `ok` | 52,196 → **5,263** | `bm25` | medium | 1.7 s |
| GitHub 404 | `not_found` | — | — | — | 2.7 s |
| arXiv PDF | `unsupported_content` | — | — | — | **10 ms** |
| Cloud metadata IP | `blocked_url` | — | — | — | **10 ms** |
| Query matches nothing | `ok` | 9,012 → 7,704 | `document_order` | none | 0.3 s |

*Fetch times are with a warm cache; a cold fetch of a large page is 2–6 s. The
two 10 ms rows never reach the network — admission rejects them before dispatch.*

### What a returned passage looks like

`web_fetch("https://en.wikipedia.org/wiki/Okapi_BM25", "what are k1 and b free
parameters")` ranks *The ranking function* first (confidence high, 5.22) and
returns it verbatim. The substring that answers the query:

> …`k 1 {\displaystyle k_{1}}` ![](…) and b are free parameters, usually chosen,
> in absence of an advanced optimization, as `k 1 ∈ [ 1.2 , 2.0 ]` … and
> `b = 0.75`.

That is the raw markdown, not cleaned up. On maths-heavy pages the LaTeX and
image markup comes through — passages are passed on **as they appear on the
page**, because the point is that you can check them against the source. The
answer (`k1 ∈ [1.2, 2.0]`, `b = 0.75`) is intact and quotable.

### The two-pass workflow, on one page

Same URL, same query — `how does multi-head attention work` on the Transformer
article (89,847 raw tokens):

| | top-ranked section | confidence | time |
|---|---|---|---|
| `mode="bm25"` | "Subsequent work" | medium (5.53) | 0.7 s |
| `mode="rerank"` | **"Multi-Query Attention"** | high (0.97) | 8.3 s |

BM25 put a section that merely mentions attention first; the cross-encoder found
the section actually about the mechanism. This is the case `mode="rerank"` exists
for — and because raw content is cached, the retry cost only the reranking, not
another crawl.

### Where it does not help much

The `document_order` row above is honest about the limit: with a 9,012-token page
and an 8,000-token budget, there is almost nothing to cut, so 7,704 tokens come
back — including navigation chrome. The tool earns its keep on **pages several
times larger than the budget**, where it drops 93% of the page. On pages just
above the budget, expect little.

## Quick start

Needs Docker and a running crawl4ai. Budget for both:

| | Image | Memory | CPU | Disk |
|---|---|---|---|---|
| **webgw** | 266 MB | 128 Mi idle, 512 Mi limit | 0.05–0.5 core | cache, 2 GB cap (4 Gi volume) |
| **crawl4ai** | ~2 GB (bundles Chromium) | **2 Gi minimum**, 4 Gi limit | 0.5–2 cores | — |

crawl4ai dominates: it runs a real browser, needs `--shm-size=1g`, and its own
health check warns as available memory falls. webgw itself is small — the work it
does is tokenising and ranking text, not rendering pages.

```bash
# 1. upstream crawler (SECRET_KEY must be >= 32 chars or gunicorn crash-loops)
docker run -d --name crawl4ai -p 11235:11235 --shm-size=1g \
  -e CRAWL4AI_API_TOKEN=my-crawl-token \
  -e SECRET_KEY=$(openssl rand -hex 32) \
  unclecode/crawl4ai:0.9.2

# 2. the gateway
docker run -d --name webgw -p 127.0.0.1:8080:8080 \
  --add-host host.docker.internal:host-gateway \
  -e CRAWL4AI_BASE_URL=http://host.docker.internal:11235 \
  -e CRAWL4AI_TOKEN=my-crawl-token \
  -e WEBGW_AUTH_TOKEN=my-gateway-token \
  -v webgw-cache:/data \
  abc99012/webgw:0.3.1

curl -s http://127.0.0.1:8080/healthz
```

`--shm-size=1g` is not optional: Chromium needs shared memory, and a container's
default `/dev/shm` of 64MB makes tabs crash at random.

From source instead:

```bash
uv sync --extra dev
uv run --extra dev pytest -q      # 82 passed
cp .env.example .env              # fill in CRAWL4AI_TOKEN
uv run python -m webgw
```

## Response format

A real response, abridged only where marked. This is the BM25 call from
[above](#what-a-returned-passage-looks-like):

```jsonc
{
  "outcome": "ok",
  "url":       "https://en.wikipedia.org/wiki/Okapi_BM25",
  "final_url": "https://en.wikipedia.org/wiki/Okapi_BM25",   // after redirects
  "status_code": 200,
  "title": "Okapi BM25 - Wikipedia",
  "fetched_at": "2026-09-04T03:25:15+00:00",

  "mode": "bm25",                 // passthrough | bm25 | rerank | document_order
  "raw_tokens": 9012,             // the whole page
  "returned_tokens": 7704,        // what you are being given
  "truncated": true,
  "cache": "miss",                // miss | hit | stale

  "retrieval": { "mode": "bm25", "elapsed_ms": 18 },

  "match": {
    "source": "bm25",             // which ranker produced these scores
    "sections_total": 8,
    "sections_scored": 8,
    "scored_ratio": 1.0,
    "top_score": 5.22,
    "score_gap": 2.07,            // top ÷ runner-up; near 1.0 means a coin flip
    "confidence": "high"          // high | medium | low | none
  },

  "excerpts": [
    {
      "section_id": "s3",
      "title": "The ranking function",
      "level": 2,
      "tokens": 1529,
      "truncated": false,
      "text": "BM25 is a bag-of-words retrieval function that ranks …"   // verbatim
    }
    // … 6 more sections, in rank order
  ],

  "outline_omitted": [
    { "id": "s8", "level": 2, "title": "External links", "tokens": 1086 }
  ]
}
```

Three fields do the work that a summary cannot:

| Field | Why it matters |
|---|---|
| `match.confidence` | Whether the query actually hit this page. `none` means nothing matched and you are getting document order, not relevance. **`bm25` and `rerank` scores are on different scales** — `source` tells you which, so don't compare a 5.22 with a 0.97 |
| `outline_omitted` | What was left out and what it would cost to ask for it. This is how the caller decides whether the answer is elsewhere, instead of guessing |
| `raw_tokens` vs `returned_tokens` | How much was actually cut. When these are close, selection did nothing for you |

`outcome` is not always `ok` — see [Outcomes](#outcomes). Treat a failure as a
failure; do not read an error page as content.

## Two decisions that shape your deployment

### 1. Is this reachable by anyone but you?

`WEBGW_AUTH_TOKEN` is the switch, and it is enforced in code, not just
documented. With no token set and a non-loopback `GATEWAY_HOST`, the server
**refuses to bind** and falls back to `127.0.0.1` with a warning.

| | Personal, local only | Served to others |
|---|---|---|
| `WEBGW_AUTH_TOKEN` | may be empty | **required** |
| Binds | `127.0.0.1` only | `0.0.0.0` |
| `MCP_ALLOWED_HOSTS` | default is fine | must list your host **including the port** |
| Transport | HTTP is fine | HTTPS — a Bearer token over plain HTTP is readable on the wire |
| Rate limiting | can relax | keep it on |

The reasoning: the one benefit of this tool confirmed by measurement is that it
crawls *from your own network*. Running it unauthenticated hands your IP
reputation to anyone who can reach the port.

### 2. Do you have a reranker?

Reranking is entirely optional. With `RERANKER_URL` empty, `mode="rerank"`
degrades to bm25 automatically rather than failing — so the tool works unchanged
with no reranker at all.

| | Self-hosted | Commercial API |
|---|---|---|
| Example | bge-reranker-v2-m3 on vLLM | Cohere, Jina, Voyage |
| `RERANKER_URL` | `http://bge-reranker:8000` | the provider's base URL |
| `RERANKER_API_KEY` | not needed | required |
| Cost | a GPU | per request |

The request and response format is the Cohere rerank shape, which the others
implement too, so the two are interchangeable — the only difference is
authentication. Compatibility requires both a `/v1/rerank` path and a `results`
key in the response; a provider returning a different top-level key needs a
one-line change in `reranker.py`.

## Retrieval modes

```
mode="bm25"    default. Keyword matching plus script normalization. ~2-3s.
mode="rerank"  BM25 top-30 shortlist -> cross-encoder rerank. ~5-8s.
```

Two-stage on purpose: the whole page is never reranked. Pages commonly hold
20–200 sections, so shortlisting first is 6x or more less model work.

**Use it in two passes.** Read once with bm25. If the passages do not contain
what you need, or `match.confidence` is `low`, retry the same URL with
`mode="rerank"`. Raw content is cached, so the retry costs only the reranking,
not another crawl.

### There is no auto mode

The intent was "rerank only when BM25 is unsure". The experiment ruled it out —
six candidate signals, all overlapping between the correct and wrong groups:

| Signal | Correct (median) | Wrong (median) |
|---|---|---|
| share of scored sections | 0.62 | 0.51 |
| top score | 12.23 | 9.28 |
| score gap | 1.64 | 1.46 |
| query term coverage | 0.88 | 0.94 |
| top-3 concentration | 0.52 | 0.43 |
| total sections | 22.50 | 95.50 |

The sharpest counterexample: `can I still build with Bazel` had a score gap of
2.09, confidence `high`, and query term coverage of 1.00 — and BM25 ranked the
answer 19th. A BM25 score says how *certain* the ranking is, not whether it is
*right*, and being confidently wrong is exactly the case worth catching.

So the decision goes to the caller, which knows things this layer does not:
whether the question matters, whether a first attempt already missed, and
whether it can afford to wait.

## Outcomes

Failures return a structured outcome instead of content. This matters because
upstream returns `success=True` with a full 404 page for a GitHub 404 — 2,434
tokens an agent would otherwise read as the answer.

| Outcome | Meaning |
|---|---|
| `ok` | content is in `excerpts` or `content` |
| `blocked_antibot` | site blocked the crawl. Not retryable |
| `not_found` | 404/410. This is an error page, not an answer |
| `unsupported_content` | PDF or binary. Not supported |
| `empty_content` | nothing retrieved, usually a JavaScript-only page |
| `timeout` | retry at most once |
| `rate_limited` | wait `retry_after_s` |
| `blocked_url` | destination not permitted |
| `blocked_redirect` | redirect landed somewhere disallowed; result discarded |

## Connecting a client

### OpenCode

In `~/.config/opencode/opencode.json` or a project-level `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "webgw": {
      "type": "remote",
      "url": "http://127.0.0.1:8080/mcp",
      "enabled": true,
      "oauth": false,
      "timeout": 180000,
      "headers": { "Authorization": "Bearer <your WEBGW_AUTH_TOKEN>" }
    }
  }
}
```

> **`timeout` is in milliseconds.** Setting `120` means 0.12 seconds:
> `tools/list` (~150ms) sits right on the edge while tool calls (2–6s) always
> time out, so the symptom is intermittent failure rather than a clean error.
> `oauth: false` is also required, or the client tries an OAuth flow first.

Check with `opencode mcp list`; the status should be `connected`.

### Claude Code

In `~/.claude.json`:

```json
{
  "mcpServers": {
    "webgw": {
      "type": "http",
      "url": "https://webgw.example.com/mcp",
      "headers": { "Authorization": "Bearer <your WEBGW_AUTH_TOKEN>" }
    }
  }
}
```

Field names differ between the two clients: `"type": "remote"` vs `"http"`, and
OpenCode needs `timeout` and `oauth` where Claude Code needs neither.

## Kubernetes

```bash
kubectl apply -f deploy/crawl4ai.yaml      # ClusterIP, never exposed
kubectl rollout status deploy/crawl4ai

kubectl apply -f deploy/k8s.yaml           # NodePort 30080
kubectl rollout status deploy/webgw

kubectl apply -f deploy/networkpolicy.yaml # optional, recommended
kubectl apply -f deploy/ingress.yaml       # when you want HTTPS
```

Change three things first: both tokens in the Secrets, and `MCP_ALLOWED_HOSTS`
so it contains `<node-ip>:<nodePort>`.

Pinned to one replica: the cache is a single SQLite file on a ReadWriteOnce PVC.
Scaling out needs RWX storage or an external cache backend.

## Configuration

All configuration is environment variables; see [.env.example](.env.example) for
the annotated list.

| Variable | Default | Notes |
|---|---|---|
| `CRAWL4AI_BASE_URL` | `http://127.0.0.1:11235` | upstream. The only thing to change when it moves |
| `CRAWL4AI_TOKEN` | — | upstream API token |
| `WEBGW_AUTH_TOKEN` | — | **empty forces a 127.0.0.1-only bind** |
| `MCP_ALLOWED_HOSTS` | `127.0.0.1,localhost` | matched against `Host`, **including the port**; mismatch returns 421 |
| `SELECT_BUDGET_TOKENS` | `8000` | measured: 92k page failed at 4000 and 6000, passed at 8000 |
| `PASSTHROUGH_MAX_TOKENS` | `4000` | smaller pages are returned whole |
| `MAX_SECTION_FRAC` | `0.35` | per-section cap; at 0.5 one page fit only 1 section |
| `RETRIEVAL_MODE` | `bm25` | default mode |
| `RERANKER_URL` | — | empty disables reranking |
| `RERANKER_MODEL` | `bge-reranker-v2-m3` | vLLM's served-model-name |
| `RERANKER_API_KEY` | — | commercial providers only |
| `RERANK_TOP_N` | `30` | shortlist size |
| `CACHE_RETENTION_DAYS` | `14` | past this the row is deleted |
| `CACHE_DEFAULT_MAX_AGE_S` | `86400` | past this the page is re-fetched, but stays available as stale |
| `MAX_CONCURRENT_FETCHES` | `4` | beyond this, requests queue |
| `RATE_LIMIT_PER_MINUTE` | `60` | 0 disables |
| `MCP_STATELESS` | `0` | keep off; clients cannot retrieve the tool list when on |

## Caching

Raw markdown is cached for 14 days, with LRU eviction under a 2GB ceiling.
Storing *raw* rather than filtered output is what makes re-querying the same page
with a different query — or a different mode — free of another crawl.

Freshness and retention are separate: past freshness the page is re-fetched but
the row survives, so when a re-fetch fails the old copy is served and flagged
`stale`. Anti-bot blocking is common, and three-day-old content beats an error
the agent cannot use.

## Security

SSRF has three layers, because upstream had four consecutive CVEs of the form
"the check exists but one path does not apply it" across 0.8.7–0.9.0:

1. `admission.py` resolves DNS and checks **every** returned address before
   dispatch, blocks private/loopback/link-local, re-validates after redirects,
   and rejects PDFs and binaries up front
2. crawl4ai 0.9.2's built-in egress pinning proxy
3. `networkpolicy.yaml`, enforced in the network rather than in application code

> NetworkPolicy requires a CNI that supports it (Calico, Cilium). **Flannel does
> not** — applying it succeeds but does nothing, which is worse than not having
> it. Verify by testing that a private address is actually blocked.

MCP transport requirements:

| Requirement | Level | Status |
|---|---|---|
| `Origin` validation (DNS rebinding) | MUST | handled by the SDK; a malicious Origin returns 403 |
| invalid protocol version returns 400 | MUST | verified |
| `GET /mcp` returns SSE or 405 | MUST | returns 405; this server never pushes |
| authenticate all connections | SHOULD | Bearer token, except `/healthz` |
| bind localhost only when local | SHOULD | forced to 127.0.0.1 with no token |

Text inside `excerpts` is web page content — untrusted external data, not
instructions. The tool description states this so connected agents are told, but
what happens next is up to the client.

## Design notes

Choices made from measurement rather than intuition:

- **`/crawl/stream`, not `/crawl`** — the same failure collapses into an opaque
  HTTP 500 on `/crawl`, while the stream endpoint names it:
  `"Blocked by anti-bot protection: DataDome captcha"`.
- **Raw markdown, not `fit_markdown`** — upstream's density filter dropped
  article headings while keeping login widgets; one page's outline went from 9
  sections to 2.
- **Document-order truncation when there is no query** — density heuristics lost
  to plain truncation at every budget tested.
- **No chrome blocklist** — navigation and footers contain no query terms, so
  they score zero and drop out on their own.
- **Script normalization** — a Traditional query against Simplified content
  otherwise fails almost entirely: of 21 sections, only the references heading
  scored, by coincidence. Same purpose as lowercasing in English retrieval.
- **CJK bigrams, not a segmenter** — no extra dependency, standard practice.
- **404s are classified here** — upstream reports `success=True` for them.

Chinese retrieval, 17 cases:

| Case | Count | rank@1 | Answer within a 4k budget |
|---|---|---|---|
| same script | 11 | 11/11 | 11/11 |
| cross script (Traditional query, Simplified content) | 5 | 2/5 | 5/5 |
| mixed Chinese/English | 1 | 1/1 | 1/1 |

## Health check

`scripts/check.py` verifies four layers from the outside in — reachability and
configuration, authentication, the MCP handshake, then real fetches. Each layer
fails for a different reason, so do not skip to the last one.

```bash
export WEBGW_URL=http://127.0.0.1:8080
export WEBGW_TOKEN=<your token>
uv run python scripts/check.py           # quick
uv run python scripts/check.py --full    # includes real fetches
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| everything returns `421`, `/healthz` fine | Host header mismatch | `MCP_ALLOWED_HOSTS` must include the port |
| `'tcp://10.x.x.x:11235' is not a valid port number` | Kubernetes injects `CRAWL4AI_PORT`, clobbering the app's own variable | `enableServiceLinks: false` |
| `sqlite3.OperationalError: unable to open database file` | PVC is root-owned, container runs as uid 10001 | `fsGroup: 10001` at **pod** level |
| crawl4ai stuck at `health: starting` | `SECRET_KEY` under 32 chars | `openssl rand -hex 32` |
| connection **times out** (not refused) | NetworkPolicy given the Service port instead of the targetPort | use the targetPort |
| client connects but sees no tools | `MCP_STATELESS` enabled | set it to `0` |
| `docker ps` says healthy but nothing connects | crawl4ai binds container loopback with no token set; its healthcheck curls from *inside* | set `CRAWL4AI_API_TOKEN` |

## Not implemented

- **The benchmark harness** — the rank@1 and Chinese-retrieval figures quoted
  above were measured during development, but the case sets and the runner are
  not in this repository, so nobody else can reproduce them. Highest priority
- Caching of rerank results — the same page with the same query pays the rerank
  cost again
- An `auto` mode — needs a signal that predicts BM25 failure; all six candidates
  were useless
- Whether the default should become `rerank` — needs real usage data
- Horizontal scaling — SQLite plus a ReadWriteOnce PVC pins this to one replica

## Versions

### Built against

| Component | Version | Notes |
|---|---|---|
| **crawl4ai** | **0.9.2** | Pinned by digest in `deploy/crawl4ai.yaml`. Not `:latest` — as of 2026-09-01 that tag had already moved to an untested build |
| MCP protocol | `2025-11-25` | Streamable HTTP transport |
| `mcp` Python SDK | `>=2.0` (tested on 2.1.1) | 2.x renamed `FastMCP` to `MCPServer`; host/port/stateless moved into `run()` |
| Python | `>=3.11` | Image ships 3.12 |

Several behaviours here are calibrated against **crawl4ai 0.9.2 specifically**
and should be re-checked when upgrading: it returns `success=True` for 404 pages,
it binds container loopback when `CRAWL4AI_API_TOKEN` is unset, its
`PruningContentFilter` drops article headings, and `/crawl` collapses anti-bot
blocks into an opaque HTTP 500 while `/crawl/stream` names them.

### Releases

Image: `abc99012/webgw:0.3.1`
(`sha256:1d69c5f5057145acb9df3f1637159de0467691710d9817a7a395c01d157954d9`)

- **0.3.1** — rerank mode no longer reuses BM25's statistics, so
  `match.confidence` reflects the ranker actually used
- 0.3.0 — script normalization, optional cross-encoder reranking, budget 8000,
  `match` signals replacing `query_matched`
- 0.2.0 — Bearer authentication, concurrency and rate limits, `GET /mcp` → 405
- 0.1.1 — fixed stateless defaulting on, which left clients unable to list tools
- 0.1.0 — has that stateless bug; do not use

## License

MIT. See [LICENSE](LICENSE).
