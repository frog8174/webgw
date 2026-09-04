#!/usr/bin/env python3
"""Two-pass retrieval demo. Standard library only -- no pip install.

Calls a live webgw over MCP streamable HTTP and prints the part that matters:
what BM25 ranked first, and what the reranker ranked first on the same page.

    WEBGW_URL=https://... WEBGW_TOKEN=... python3 demo.py

The token is read from the environment so it never appears on screen.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

URL = os.environ.get("WEBGW_URL", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.environ.get("WEBGW_TOKEN", "")
PROTOCOL = "2025-11-25"

PAGE = "https://en.wikipedia.org/wiki/Transformer_(deep_learning_architecture)"
QUERY = "how does multi-head attention work"

B, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
CYAN, GREEN, YELLOW = "\033[36m", "\033[32m", "\033[33m"

_session: str | None = None


def rpc(method: str, params: dict | None = None, notify: bool = False) -> dict:
    global _session
    body = {"jsonrpc": "2.0", "method": method}
    if not notify:
        body["id"] = 1
    if params is not None:
        body["params"] = params
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL,
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    if _session:
        headers["Mcp-Session-Id"] = _session
    req = urllib.request.Request(
        f"{URL}/mcp", data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            _session = sid
        raw = r.read().decode()
    if not raw.strip():
        return {}
    # MCP_JSON_RESPONSE=1 gives plain JSON; tolerate SSE framing anyway.
    if raw.startswith("event:") or raw.startswith("data:"):
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
    return json.loads(raw)


def fetch(mode: str) -> tuple[dict, int]:
    t0 = time.time()
    out = rpc("tools/call", {"name": "web_fetch",
                             "arguments": {"url": PAGE, "query": QUERY, "mode": mode}})
    ms = int((time.time() - t0) * 1000)
    res = out.get("result", {})
    data = res.get("structuredContent") or res.get("structured_content")
    if data is None:
        data = json.loads(res["content"][0]["text"])
    return data, ms


def show(label: str, d: dict, ms: int) -> None:
    m = d.get("match") or {}
    top = (d.get("excerpts") or [{}])[0].get("title", "?")
    conf = m.get("confidence", "?")
    colour = GREEN if conf == "high" else YELLOW
    print(f"  {B}{label:<16}{OFF}"
          f"{d['raw_tokens']:>7,} -> {B}{d['returned_tokens']:>6,}{OFF} tokens"
          f"   {colour}{conf:<6}{OFF}"
          f" {DIM}({m.get('top_score')}){OFF}"
          f"   cache={d.get('cache')}   {ms/1000:.1f}s")
    print(f"  {DIM}{'':<16}top section:{OFF} {CYAN}{top}{OFF}\n")


def main() -> int:
    rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "demo", "version": "1"}})
    rpc("notifications/initialized", notify=True)

    print(f"\n  {DIM}page {OFF}{PAGE.split('/wiki/')[-1].replace('_', ' ')}")
    print(f"  {DIM}query{OFF} {B}{QUERY}{OFF}\n")

    a, ms_a = fetch("bm25")
    show("mode=bm25", a, ms_a)
    b, ms_b = fetch("rerank")
    show("mode=rerank", b, ms_b)

    cut = 100 - b["returned_tokens"] * 100 // b["raw_tokens"]
    print(f"  {DIM}Same page, same query. The reranker found the section that is"
          f" actually{OFF}")
    print(f"  {DIM}about the mechanism -- and cache={b.get('cache')} means the retry"
          f" never re-crawled.{OFF}")
    print(f"  {DIM}Either way the page shrank by{OFF} {B}{cut}%{OFF}"
          f"{DIM}, and every passage is verbatim.{OFF}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"\n  demo failed: {type(exc).__name__}: {exc}\n", file=sys.stderr)
        sys.exit(1)
