"""結果判定。把上游回應歸類成明確的 outcome。

這是整個 gateway 最重要的一層。實測到的靜默失敗:

  * GitHub 404 頁 -> success=True, status_code=404, 回傳完整 404 頁面 2,434 tokens
    agent 會把錯誤頁當內容讀,產生看似合理的錯誤答案。

  * 反爬阻擋 -> /crawl 端點收斂成不透明的 HTTP 500,指名的判定字串只寫在容器日誌。
    改走 /crawl/stream 就能拿到 in-band 的 error_message:
        "Blocked by anti-bot protection: DataDome captcha"   (Reuters)
        "Blocked by anti-bot protection: PerimeterX block"   (Bloomberg)
        "Blocked by anti-bot protection: Cloudflare JS challenge" (Medium)
    這是選 /crawl/stream 而非 /crawl 的主要理由。
"""
from __future__ import annotations

from dataclasses import dataclass

OK = "ok"
BLOCKED_ANTIBOT = "blocked_antibot"
NOT_FOUND = "not_found"
HTTP_ERROR = "http_error"
UNSUPPORTED_CONTENT = "unsupported_content"
EMPTY_CONTENT = "empty_content"
TIMEOUT = "timeout"
FETCH_FAILED = "fetch_failed"
BLOCKED_URL = "blocked_url"
BLOCKED_REDIRECT = "blocked_redirect"
RATE_LIMITED = "rate_limited"

# 低於此長度視為沒有實質內容。
# 校準依據:Reuters 被 DataDome 擋時 raw_markdown 只有 1 個字元;
# 而 example.com 是合法的小頁面,只有 165 字元 —— 門檻設 200 會誤殺它。
# 取 50:足以分辨「幾乎什麼都沒有」與「小但真實」。
MIN_CONTENT_CHARS = 50

RETRYABLE = {TIMEOUT, FETCH_FAILED, RATE_LIMITED}

_HINTS = {
    BLOCKED_ANTIBOT: "站台以反爬機制阻擋。重試不會成功 (實測同一頁連打 4 次全被擋),請改用其他來源。",
    NOT_FOUND: "頁面不存在。回傳內容是錯誤頁而非資料,不要據此作答。",
    HTTP_ERROR: "伺服器回傳錯誤狀態碼,取得的內容不可信。",
    UNSUPPORTED_CONTENT: "此類型檔案 (PDF/二進位) 本工具不支援。",
    EMPTY_CONTENT: "抓到的內容為空,通常表示頁面需要 JavaScript 或被靜默阻擋。",
    TIMEOUT: "抓取逾時,可重試一次。",
    FETCH_FAILED: "無法連上上游抓取服務。",
    BLOCKED_URL: "目的地不允許存取。不要改寫 URL 嘗試繞過。",
    BLOCKED_REDIRECT: "轉址後的落點不在允許範圍內,結果已丟棄。",
    RATE_LIMITED: "請求過於頻繁,已達速率上限。稍後再試,或降低呼叫頻率。",
}


@dataclass
class Outcome:
    code: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.code == OK

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    @property
    def hint(self) -> str:
        return _HINTS.get(self.code, "")


def classify(result: dict, markdown: str) -> Outcome:
    """依上游單筆結果判定。result 是 /crawl/stream 的一行 JSON。"""
    err = (result.get("error_message") or "").strip()
    status = result.get("status_code")

    if err:
        low = err.lower()
        if "anti-bot" in low or "antibot" in low:
            # 保留上游指名的協定 (DataDome / PerimeterX / Cloudflare JS challenge)
            detail = err.split(":", 1)[-1].strip() if ":" in err else err
            return Outcome(BLOCKED_ANTIBOT, detail[:160])
        if "download is starting" in low:
            return Outcome(UNSUPPORTED_CONTENT, "上游嘗試下載檔案而非渲染頁面")
        if "timeout" in low or "timed out" in low:
            return Outcome(TIMEOUT, err[:160])
        return Outcome(FETCH_FAILED, err[:160])

    if isinstance(status, int):
        if status in (404, 410):
            return Outcome(NOT_FOUND, f"HTTP {status}")
        # 3xx 是正常的轉址,由 admission.check_redirect 另外把關。
        if status >= 400:
            return Outcome(HTTP_ERROR, f"HTTP {status}")

    if not result.get("success", False):
        return Outcome(FETCH_FAILED, "upstream reported success=false")

    if len(markdown.strip()) < MIN_CONTENT_CHARS:
        return Outcome(EMPTY_CONTENT, f"only {len(markdown.strip())} chars")

    return Outcome(OK)
