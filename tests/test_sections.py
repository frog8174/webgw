"""章節切分與目錄偵測。

目錄偵測的動機來自中文驗證:中文維基查「自注意力機制的背景」時,「目录」排第 2、
真正的答案排第 5 —— 目錄含有全頁標題,對任何查詢都高分,卻沒有任何內容。
加入偵測後,同字集查詢的 rank@1 從 6/11 升到 11/11。
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
    """維基目錄項帶編號(「1 背景」)而標題是「背景」,正規化要能對上。"""
    titles = ["背景", "架構", "訓練", "應用"]
    body = _toc([f"{i} {t}" for i, t in enumerate(titles, 1)])
    assert sections.is_index_section(body, [sections._norm_title(t) for t in titles]) is True


def test_prose_section_with_links_is_not_index():
    """論文的交叉引用章節不可被誤判 —— ar5iv 的「6.1 Machine Translation」曾中招。"""
    titles = ["1 introduction", "6.1 machine translation", "5.3 optimizer"]
    body = _prose("translation") + "\n[Table 2](#t2) [Section 3](#s3)"
    assert sections.is_index_section(body, titles) is False


def test_link_heavy_section_without_heading_coverage_is_not_index():
    """全是連結但不覆蓋標題的(例如相關文章清單),不是目錄。"""
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
    """標題行不在 body 裡,若在切分時截斷會讓超出的文字永久消失於索引之外。"""
    long_title = "A" * 300
    md = f"## {long_title}\n" + _prose("content")
    sec = sections.split(md)[0]
    assert len(sec.title) == 300                                   # 檢索用完整標題
    assert len(sec.display_title) == sections.TITLE_DISPLAY_CHARS   # 顯示用截斷
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
    """stateless 時伺服器不發 Mcp-Session-Id,實測 OpenCode 會取不到工具清單。

    預設必須關閉 —— 快取是 SQLite 單檔、replicas 鎖在 1,stateless 換不到擴展性。
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
    """GET /mcp 必須回 405,其餘路徑不受影響。

    那條 SSE 串流是選用的伺服器→客戶端推送通道,本服務從不推送。
    SDK 預設回 200 並永久佔住一條 HTTP/1.1 連線,實測會讓 OpenCode 約半數
    連線卡死(tools/list 排在串流後面,永遠送不出去)。
    """
    from webgw.server import DenyStandaloneGet

    seen = {}

    async def inner(scope, receive, send):
        seen["passed_through"] = scope["path"]

    sent = []

    async def send(msg):
        sent.append(msg)

    wrapped = DenyStandaloneGet(inner)

    # GET /mcp -> 405,不進入內層
    await wrapped({"type": "http", "method": "GET", "path": "/mcp"}, None, send)
    assert sent[0]["status"] == 405
    assert ("passed_through" in seen) is False

    # POST /mcp -> 放行
    await wrapped({"type": "http", "method": "POST", "path": "/mcp"}, None, send)
    assert seen["passed_through"] == "/mcp"

    # GET /healthz -> 放行 (探針不能被擋掉)
    await wrapped({"type": "http", "method": "GET", "path": "/healthz"}, None, send)
    assert seen["passed_through"] == "/healthz"
