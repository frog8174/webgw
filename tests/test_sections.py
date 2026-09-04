"""Section splitting and table-of-contents detection.

TOC detection came out of the Chinese validation set: on Chinese Wikipedia, a
query about the background of self-attention put the TOC 2nd and the real answer
5th -- a TOC contains every heading on the page, so it scores highly against any
query while carrying no content. Adding detection moved same-script rank@1 from
6/11 to 11/11.

The Chinese strings below are test data, not prose: TOC detection has to work
without any language-specific keywords, and these cases are what proves it.
"""
from __future__ import annotations

from webgw import sections


def _toc(entries: list[str]) -> str:
    return "\n".join(f"* [{e}](#anchor-{i})" for i, e in enumerate(entries))


def _prose(word: str, n: int = 80) -> str:
    return (word + " ") * n


def test_index_section_detected_by_structure():
    titles = ["背景", "架構", "訓練", "應用", "實現"]
    body = _toc(titles)
    assert sections.is_index_section(body, [sections._norm_title(t) for t in titles]) is True


def test_toc_with_leading_numbers_still_detected():
    """Wikipedia TOC entries carry numbers ("1 Background") where the heading
    does not, so normalization has to bring them together."""
    titles = ["背景", "架構", "訓練", "應用"]
    body = _toc([f"{i} {t}" for i, t in enumerate(titles, 1)])
    assert sections.is_index_section(body, [sections._norm_title(t) for t in titles]) is True


def test_prose_section_with_links_is_not_index():
    """Cross-reference sections in papers must not be misclassified -- ar5iv's
    "6.1 Machine Translation" was caught by an earlier version."""
    titles = ["1 introduction", "6.1 machine translation", "5.3 optimizer"]
    body = _prose("translation") + "\n[Table 2](#t2) [Section 3](#s3)"
    assert sections.is_index_section(body, titles) is False


def test_link_heavy_section_without_heading_coverage_is_not_index():
    """All links but no heading coverage (a related-articles list, say) is not a TOC."""
    body = _toc(["其他文章一", "其他文章二", "其他文章三"])
    assert sections.is_index_section(body, ["背景", "架構", "訓練"]) is False


def test_split_drops_index_section():
    md = (
        "## 目錄\n" + _toc(["背景", "架構", "訓練"]) + "\n"
        "## 背景\n" + _prose("背景說明") + "\n"
        "## 架構\n" + _prose("架構說明") + "\n"
        "## 訓練\n" + _prose("訓練說明") + "\n"
    )
    titles = [s.title for s in sections.split(md)]
    assert "目錄" not in titles
    assert {"背景", "架構", "訓練"} <= set(titles)


def test_title_truncation_is_display_only():
    """The heading line is not part of the body, so truncating at split time
    would hide the overflow from the index permanently."""
    long_title = "A" * 300
    md = f"## {long_title}\n" + _prose("content")
    sec = sections.split(md)[0]
    assert len(sec.title) == 300                                   # full title for retrieval
    assert len(sec.display_title) == sections.TITLE_DISPLAY_CHARS   # truncated for display
    assert len(sec.as_outline_entry()["title"]) == sections.TITLE_DISPLAY_CHARS


def test_tiny_fragments_dropped():
    md = "## A\n短\n## B\n" + _prose("內容")
    titles = [s.title for s in sections.split(md)]
    assert "A" not in titles and "B" in titles


def test_section_tokens_recorded():
    md = "## A\n" + _prose("word")
    sec = sections.split(md)[0]
    assert sec.tokens > 0


def test_stateless_defaults_off(monkeypatch):
    """Stateless issues no Mcp-Session-Id, and OpenCode was measured unable to
    retrieve the tool list as a result.

    It must default to off: the cache is a single SQLite file with replicas
    pinned to 1, so stateless buys no scalability in exchange.
    """
    import importlib

    from webgw import config

    monkeypatch.delenv("MCP_STATELESS", raising=False)
    importlib.reload(config)
    assert config.Config().mcp_stateless is False

    monkeypatch.setenv("MCP_STATELESS", "1")
    importlib.reload(config)
    assert config.Config().mcp_stateless is True
    monkeypatch.delenv("MCP_STATELESS", raising=False)
    importlib.reload(config)


async def test_get_mcp_returns_405_but_other_paths_pass():
    """GET /mcp must answer 405, and every other path must be unaffected.

    That SSE stream is the optional server-to-client push channel, which this
    service never uses. The SDK's default answers 200 and occupies an HTTP/1.1
    connection indefinitely, which was measured stalling about half of
    OpenCode's connections -- tools/list queued behind the stream and never got
    sent.
    """
    from webgw.server import DenyStandaloneGet

    seen = {}

    async def inner(scope, receive, send):
        seen["passed_through"] = scope["path"]

    sent = []

    async def send(msg):
        sent.append(msg)

    wrapped = DenyStandaloneGet(inner)

    # GET /mcp -> 405, never reaching the inner app
    await wrapped({"type": "http", "method": "GET", "path": "/mcp"}, None, send)
    assert sent[0]["status"] == 405
    assert ("passed_through" in seen) is False

    # POST /mcp -> passes through
    await wrapped({"type": "http", "method": "POST", "path": "/mcp"}, None, send)
    assert seen["passed_through"] == "/mcp"

    # GET /healthz -> passes through (probes must not be blocked)
    await wrapped({"type": "http", "method": "GET", "path": "/healthz"}, None, send)
    assert seen["passed_through"] == "/healthz"
