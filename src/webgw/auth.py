"""Bearer token authentication.

The transport security section of the MCP specification lists three
requirements; the third is that a server SHOULD implement proper
authentication for all connections. Before this layer existed the gateway
served anyone who could reach the port.

Why it matters here: the one benefit of this tool confirmed by measurement is
that it crawls *from your own network* -- in the Medium case, a residential IP
succeeded where a datacenter IP got a 403. Running without authentication hands
your IP reputation to anyone who can reach that port.

Binding safeguard: with no token set, the server is forced onto 127.0.0.1 only
(see config.effective_host). That mirrors upstream crawl4ai 0.9.2, which
refuses to bind 0.0.0.0 without an API token -- it rules out the most dangerous
combination, publicly reachable and unauthenticated.
"""
from __future__ import annotations

import hmac
import logging

log = logging.getLogger("webgw.auth")

# Exempt from authentication: Kubernetes liveness and readiness probes have to
# reach this without credentials.
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
    """ASGI wrapper that checks `Authorization: Bearer <token>`.

    Disabled entirely when the token is an empty string, which is the
    single-machine loopback development case.
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
        # compare_digest rather than ==: ordinary string comparison returns at
        # the first differing character, letting an attacker recover the token
        # one character at a time by timing the responses.
        if presented is None or not hmac.compare_digest(presented, self._token):
            log.warning(
                "unauthorized request path=%s client=%s",
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
