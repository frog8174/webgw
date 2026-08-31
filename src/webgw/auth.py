"""Bearer token 認證。

MCP 規格的傳輸安全章節列出三條要求,其中第三條是伺服器**應該**(SHOULD)
對所有連線實作適當認證。在此之前 gateway 對任何連得到該埠的人全部放行。

為什麼一定要有:這個工具唯一被實測證實的價值,是「從你自己的網路出去爬」——
Medium 那個案例中,你的 IP 通、資料中心 IP 被 403。沒有認證等於把你的 IP 信譽
開放給任何能連到那個埠的人使用。

綁定安全機制:沒設 token 時強制只綁 127.0.0.1(見 config.effective_host)。
這是照抄上游 crawl4ai 0.9.2 的做法 —— 它在沒有 API token 時拒絕綁 0.0.0.0,
正好擋掉「對外開放 + 無認證」這個最危險的組合。
"""
from __future__ import annotations

import hmac
import logging

log = logging.getLogger("webgw.auth")

# 這些路徑不需認證:k8s 的存活/就緒探針必須能在沒有憑證的情況下打。
PUBLIC_PATHS = frozenset({"/healthz"})


def _extract_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name.lower() != b"authorization":
            continue
        raw = value.decode("latin-1").strip()
        scheme, _, token = raw.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token.strip()
    return None


class BearerAuth:
    """ASGI 包裝層:檢查 Authorization: Bearer <token>。

    token 為空字串時完全不啟用(單機 loopback 開發用)。
    """

    def __init__(self, app, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope, receive, send) -> None:
        if not self._token or scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        if scope.get("path", "").rstrip("/") in PUBLIC_PATHS or scope.get("path") == "/healthz":
            await self._app(scope, receive, send)
            return

        presented = _extract_token(scope.get("headers") or [])
        # 用 compare_digest 而非 == :一般字串比較會在第一個不同的字元就返回,
        # 攻擊者可藉由測量回應時間逐字元猜出 token。
        if presented is None or not hmac.compare_digest(presented, self._token):
            log.warning(
                "未授權的請求 path=%s client=%s",
                scope.get("path"), (scope.get("client") or ("?",))[0],
            )
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"www-authenticate", b'Bearer realm="webgw"'),
                ],
            })
            await send({"type": "http.response.body", "body": b"Unauthorized"})
            return

        await self._app(scope, receive, send)
