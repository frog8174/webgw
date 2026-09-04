# -*- coding: utf-8 -*-
"""webgw service health check.

Usage:
    export WEBGW_URL=https://webgw.example.com
    export WEBGW_TOKEN=<your token>
    python scripts/check.py            # quick check
    python scripts/check.py --full     # also perform real fetches (goes online)

Layered by design: each layer fails for a different reason, so they are
confirmed from the outside in. Do not skip ahead to the last step -- that leaves
you unable to tell which part actually broke.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = os.environ.get("WEBGW_URL", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.environ.get("WEBGW_TOKEN", "")  # supplied via the environment; never hardcode
HDR = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}

OK, FAIL, WARN = "  [ OK ]", "  [FAIL]", "  [WARN]"


def line(status: str, label: str, detail: str = "") -> None:
    print(f"{status} {label:<34}{detail}")


async def layer1_reachable() -> dict | None:
    """Layer 1: the service is alive and the configuration took effect.

    /healthz needs no authentication (the Kubernetes probes use it), so a
    failure here points at the network or the Ingress, not at the token.
    """
    print("\n-- Layer 1: reachability and configuration --")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{URL}/healthz")
        if r.status_code != 200:
            line(FAIL, "/healthz", f"HTTP {r.status_code}")
            return None
        h = r.json()
    except Exception as e:  # noqa: BLE001
        line(FAIL, "/healthz", f"{type(e).__name__}: {str(e)[:60]}")
        return None

    line(OK, "service alive", h.get("status"))
    line(OK, "upstream crawl4ai", h.get("upstream"))
    line(OK if h.get("auth") == "enabled" else WARN, "authentication",
         h.get("auth") + ("" if h.get("auth") == "enabled" else "  <- required when public"))
    rt = h.get("retrieval") or {}
    if rt:
        line(OK, "default retrieval mode", rt.get("default_mode"))
        line(OK, "selection budget", f"{rt.get('budget_tokens')} tokens")
        line(OK if rt.get("reranker") else WARN, "reranker",
             rt.get("reranker") or "not configured (mode=rerank will degrade to bm25)")
    else:
        line(WARN, "version", "no retrieval block -> deployed build predates 0.3.0")
    c = h.get("cache") or {}
    line(OK, "cache", f"retention {c.get('retention_days')} days  {c.get('path')}")
    return h


async def layer2_auth() -> bool:
    """Layer 2: authentication is actually rejecting.

    Without a token the answer should be 401. A 421 means MCP_ALLOWED_HOSTS does
    not include your domain.
    """
    print("\n-- Layer 2: authentication --")
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    hdrs = {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{URL}/mcp", json=body, headers=hdrs)
    except Exception as e:  # noqa: BLE001
        line(FAIL, "unauthorized request", f"{type(e).__name__}")
        return False

    if r.status_code == 401:
        line(OK, "request without token rejected", "401")
        return True
    if r.status_code == 421:
        line(FAIL, "Host check", "421 -> MCP_ALLOWED_HOSTS is missing your domain")
        return False
    line(WARN, "request without token", f"HTTP {r.status_code}  <- expected 401; auth may be off")
    return True


async def layer3_mcp() -> dict | None:
    """Layer 3: the MCP handshake, and the tool interface an agent actually sees.

    The parameter list is the most reliable way to identify the deployed
    version -- it checks what the agent really receives, which beats reading pod
    status or image tags.
    """
    print("\n-- Layer 3: MCP protocol --")
    try:
        async with httpx.AsyncClient(headers=HDR, timeout=60) as hc:
            async with streamable_http_client(f"{URL}/mcp", http_client=hc) as st:
                async with ClientSession(st[0], st[1]) as s:
                    init = await s.initialize()
                    tools = (await s.list_tools()).tools
    except BaseException as e:  # noqa: BLE001
        sub = (getattr(e, "exceptions", None) or [e])[0]
        line(FAIL, "handshake", f"{type(sub).__name__}: {str(sub)[:60]}")
        return None

    t = tools[0]
    params = list(t.input_schema.get("properties", {}))
    line(OK, "handshake + tools/list", f"{init.server_info.name} v{init.server_info.version}")
    line(OK, "tool parameters", str(params))
    line(OK if "mode" in params else WARN, "mode switching supported",
         "yes" if "mode" in params else "no -> deployed build is not 0.3.0+")
    return {"params": params}


async def layer4_fetch() -> None:
    """Layer 4: real fetches. Only this layer exercises gateway -> crawl4ai.

    When the first three layers pass and this one fails, it is almost always
    CRAWL4AI_BASE_URL pointing somewhere wrong.
    """
    print("\n-- Layer 4: real fetches --")
    cases = [
        ("https://example.com", None, None, "small page"),
        ("https://en.wikipedia.org/wiki/Okapi_BM25", "k1 b free parameters", None, "selection"),
        ("https://zh.wikipedia.org/wiki/Transformer%E6%A8%A1%E5%9E%8B",
         "編碼器 解碼器 架構", None, "cross-script CJK"),
        ("https://zh.wikipedia.org/wiki/Transformer%E6%A8%A1%E5%9E%8B",
         "編碼器 解碼器 架構", "rerank", "reranking"),
        ("https://arxiv.org/pdf/1706.03762.pdf", None, None, "PDF (should be blocked)"),
    ]
    async with httpx.AsyncClient(headers=HDR, timeout=200) as hc:
        async with streamable_http_client(f"{URL}/mcp", http_client=hc) as st:
            async with ClientSession(st[0], st[1]) as s:
                await s.initialize()
                for u, q, mode, label in cases:
                    args = {"url": u}
                    if q:
                        args["query"] = q
                    if mode:
                        args["mode"] = mode
                    t0 = time.time()
                    try:
                        r = await s.call_tool("web_fetch", args)
                        d = r.structured_content or json.loads(r.content[0].text)
                    except BaseException as e:  # noqa: BLE001
                        line(FAIL, label, f"{type(e).__name__}")
                        continue
                    ms = int((time.time() - t0) * 1000)
                    oc = d.get("outcome")
                    good = oc == "ok" or (label.startswith("PDF") and oc == "unsupported_content")
                    extra = (f"{d.get('mode')} {d.get('raw_tokens')}->{d.get('returned_tokens')}"
                             f"  cache={d.get('cache')}" if oc == "ok" else (d.get("detail") or "")[:36])
                    line(OK if good else FAIL, f"{label} ({ms}ms)", f"{oc}  {extra}")
                    if d.get("retrieval", {}).get("degraded"):
                        line(WARN, "  rerank degraded", d["retrieval"]["degraded"][:50])


async def main() -> None:
    full = "--full" in sys.argv
    print(f"target: {URL}")
    if not TOKEN:
        print("  (WEBGW_TOKEN is unset; layers 3 and 4 will fail)")

    if await layer1_reachable() is None:
        print("\nLayer 1 failed; stop here -- check the Ingress and pod status first.")
        return
    await layer2_auth()
    if await layer3_mcp() is None:
        print("\nLayer 3 failed -- check the token and MCP_ALLOWED_HOSTS.")
        return
    if full:
        await layer4_fetch()
    else:
        print("\n(pass --full to also run real fetch tests)")


if __name__ == "__main__":
    asyncio.run(main())
