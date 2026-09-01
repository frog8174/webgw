# -*- coding: utf-8 -*-
"""webgw 服務健檢。

用法:
    export WEBGW_URL=https://webgw.example.com
    export WEBGW_TOKEN=<你的 token>
    python scripts/check.py            # 快速檢查
    python scripts/check.py --full     # 加上實際抓取(會對外連線)

分層設計:每一層失敗的原因不同,由外而內逐層確認,
不要跳過前面的層直接看最後一步 —— 那會讓你分不清是哪一段壞了。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = os.environ.get("WEBGW_URL", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.environ.get("WEBGW_TOKEN", "")
HDR = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}

OK, FAIL, WARN = "  [OK]  ", "  [失敗]", "  [注意]"


def line(status: str, label: str, detail: str = "") -> None:
    print(f"{status} {label:<34}{detail}")


async def layer1_reachable() -> dict | None:
    """第 1 層:服務活著,而且設定有吃進去。

    /healthz 不需認證(k8s 探針要用),所以這一層失敗代表網路或 Ingress 有問題,
    跟 token 無關。
    """
    print("\n── 第 1 層:可達性與設定 ──")
    try:
        async with httpx2.AsyncClient(timeout=15) as c:
            r = await c.get(f"{URL}/healthz")
        if r.status_code != 200:
            line(FAIL, "/healthz", f"HTTP {r.status_code}")
            return None
        h = r.json()
    except Exception as e:  # noqa: BLE001
        line(FAIL, "/healthz", f"{type(e).__name__}: {str(e)[:60]}")
        return None

    line(OK, "服務存活", h.get("status"))
    line(OK, "上游 crawl4ai", h.get("upstream"))
    line(OK if h.get("auth") == "enabled" else WARN, "認證",
         h.get("auth") + ("" if h.get("auth") == "enabled" else "  <- 對外服務務必開啟"))
    rt = h.get("retrieval") or {}
    if rt:
        line(OK, "檢索預設模式", rt.get("default_mode"))
        line(OK, "選節預算", f"{rt.get('budget_tokens')} tokens")
        line(OK if rt.get("reranker") else WARN, "重排服務",
             rt.get("reranker") or "未設定(mode=rerank 會降級為 bm25)")
    else:
        line(WARN, "版本", "沒有 retrieval 區塊 -> 線上是 0.2.x 以前的版本")
    c = h.get("cache") or {}
    line(OK, "快取", f"保留 {c.get('retention_days')} 天  {c.get('path')}")
    return h


async def layer2_auth() -> bool:
    """第 2 層:認證真的在擋。

    不帶 token 應該回 401。回 421 的話是 MCP_ALLOWED_HOSTS 沒有含你的網域。
    """
    print("\n── 第 2 層:認證 ──")
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    hdrs = {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}
    try:
        async with httpx2.AsyncClient(timeout=15) as c:
            r = await c.post(f"{URL}/mcp", json=body, headers=hdrs)
    except Exception as e:  # noqa: BLE001
        line(FAIL, "未授權請求", f"{type(e).__name__}")
        return False

    if r.status_code == 401:
        line(OK, "未帶 token 被拒", "401")
        return True
    if r.status_code == 421:
        line(FAIL, "Host 檢查", "421 -> MCP_ALLOWED_HOSTS 缺少你的網域")
        return False
    line(WARN, "未帶 token", f"HTTP {r.status_code}  <- 預期 401,認證可能沒開")
    return True


async def layer3_mcp() -> dict | None:
    """第 3 層:MCP 協定握手,以及 agent 實際看到的工具介面。

    參數列表是判斷版本最可靠的方式 —— 它驗的是 agent 真正會拿到的東西,
    比看 Pod 狀態或映像標籤都準。
    """
    print("\n── 第 3 層:MCP 協定 ──")
    try:
        async with httpx2.AsyncClient(headers=HDR, timeout=60) as hc:
            async with streamable_http_client(f"{URL}/mcp", http_client=hc) as st:
                async with ClientSession(st[0], st[1]) as s:
                    init = await s.initialize()
                    tools = (await s.list_tools()).tools
    except BaseException as e:  # noqa: BLE001
        sub = (getattr(e, "exceptions", None) or [e])[0]
        line(FAIL, "握手", f"{type(sub).__name__}: {str(sub)[:60]}")
        return None

    t = tools[0]
    params = list(t.input_schema.get("properties", {}))
    line(OK, "握手 + tools/list", f"{init.server_info.name} v{init.server_info.version}")
    line(OK, "工具參數", str(params))
    line(OK if "mode" in params else WARN, "支援 mode 切換",
         "是" if "mode" in params else "否 -> 線上不是 0.3.0")
    return {"params": params}


async def layer4_fetch() -> None:
    """第 4 層:實際抓取。這一層才會驗到 gateway -> crawl4ai 的連線。

    前三層全過但這一層失敗,幾乎都是 CRAWL4AI_BASE_URL 設錯。
    """
    print("\n── 第 4 層:實際抓取 ──")
    cases = [
        ("https://example.com", None, None, "小頁面"),
        ("https://en.wikipedia.org/wiki/Okapi_BM25", "k1 b free parameters", None, "選節"),
        ("https://zh.wikipedia.org/wiki/Transformer%E6%A8%A1%E5%9E%8B",
         "編碼器 解碼器 架構", None, "繁簡跨字集"),
        ("https://zh.wikipedia.org/wiki/Transformer%E6%A8%A1%E5%9E%8B",
         "編碼器 解碼器 架構", "rerank", "重排"),
        ("https://arxiv.org/pdf/1706.03762.pdf", None, None, "PDF(應被擋)"),
    ]
    async with httpx2.AsyncClient(headers=HDR, timeout=200) as hc:
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
                        line(WARN, "  重排降級", d["retrieval"]["degraded"][:50])


async def main() -> None:
    full = "--full" in sys.argv
    print(f"目標: {URL}")
    if not TOKEN:
        print("  (未設 WEBGW_TOKEN,第 3、4 層會失敗)")

    if await layer1_reachable() is None:
        print("\n第 1 層失敗,後面不用測了 —— 先確認 Ingress 與 Pod 狀態。")
        return
    await layer2_auth()
    if await layer3_mcp() is None:
        print("\n第 3 層失敗 —— 檢查 token 與 MCP_ALLOWED_HOSTS。")
        return
    if full:
        await layer4_fetch()
    else:
        print("\n(加 --full 可執行實際抓取測試)")


if __name__ == "__main__":
    asyncio.run(main())
