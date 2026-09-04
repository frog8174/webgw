#!/usr/bin/env python3
"""Two-pass retrieval demo. Standard library only -- no pip install.

Calls a live webgw over MCP streamable HTTP and shows what each mode actually
returned, then checks the returned text for the terms any correct answer to the
query has to contain. Section titles alone prove nothing; the check is the point.

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

# Terms an explanation of the mechanism has to use. Checked against the text
# actually returned -- this is what makes the demo evidence rather than a claim.
MUST = ["query", "key", "value", "softmax", "dot-product", "concaten"]

B, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"
CYAN, GREEN, YELLOW, RED = "\033[36m", "\033[32m", "\033[33m", "\033[31m"

_session: str | None = None


def rpc(method: str, params: dict | None = None, notify: bool = False) -> dict:
    global _session
    body: dict = {"jsonrpc": "2.0", "method": method}
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
    if raw.startswith(("event:", "data:")):
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
    return json.loads(raw)


def fetch(mode: str) -> tuple[dict, float]:
    t0 = time.time()
    out = rpc("tools/call", {"name": "web_fetch",
                             "arguments": {"url": PAGE, "query": QUERY, "mode": mode}})
    res = out.get("result", {})
    data = res.get("structuredContent") or res.get("structured_content")
    if data is None:
        data = json.loads(res["content"][0]["text"])
    return data, time.time() - t0


def show(mode: str, d: dict, secs: float) -> None:
    m = d.get("match") or {}
    conf = m.get("confidence", "?")
    ccol = GREEN if conf == "high" else YELLOW
    print(f"  {B}mode={mode:<7}{OFF}{DIM}->{OFF} {B}{d['returned_tokens']:>5,}{OFF} tok"
          f"   {ccol}{conf:<6}{OFF}{DIM}({m.get('top_score')}){OFF}   {secs:.1f}s")

    joined = " ".join(e["text"] for e in d.get("excerpts") or []).lower()
    for i, e in enumerate(d.get("excerpts") or [], 1):
        hit = any(t in e["text"].lower() for t in MUST)
        mark = f"{GREEN}*{OFF}" if hit else " "
        print(f"   {mark}{i} {e['title'][:34]:<36}{DIM}{e['tokens']:>5,} tok{OFF}")

    found = [t for t in MUST if t in joined]
    if len(found) >= 4:
        verdict, col = "answerable", GREEN
    elif found:
        verdict, col = "partial", YELLOW
    else:
        verdict, col = "NOT answerable", RED
    print(f"     {DIM}mechanism terms:{OFF} {col}{', '.join(found) or 'none'}"
          f"  -> {verdict}{OFF}\n")


def main() -> int:
    rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "demo", "version": "1"}})
    rpc("notifications/initialized", notify=True)

    a, sa = fetch("bm25")
    print(f"\n  {DIM}page {OFF}Transformer (deep learning)   "
          f"{B}{a['raw_tokens']:,}{OFF}{DIM} tokens raw{OFF}")
    print(f"  {DIM}query{OFF} {B}{QUERY}{OFF}\n")
    show("bm25", a, sa)
    b, sb = fetch("rerank")
    show("rerank", b, sb)
    print(f"  {DIM}Same page, same query. cache={b.get('cache')} -- the retry never"
          f" re-crawled.{OFF}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"\n  demo failed: {type(exc).__name__}: {exc}\n", file=sys.stderr)
        sys.exit(1)
