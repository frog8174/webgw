"""Outcome classification. Sorts upstream responses into explicit outcomes.

This is the most important layer in the gateway. Silent failures observed in
testing:

  * A GitHub 404 page -> success=True, status_code=404, and a full 404 page
    returned as 2,434 tokens of content. An agent reads the error page as
    content and produces a plausible-sounding wrong answer.

  * Anti-bot blocks -> the /crawl endpoint collapses these into an opaque
    HTTP 500, naming the protection only in the container log. Switching to
    /crawl/stream surfaces the error_message in band:
        "Blocked by anti-bot protection: DataDome captcha"        (Reuters)
        "Blocked by anti-bot protection: PerimeterX block"        (Bloomberg)
        "Blocked by anti-bot protection: Cloudflare JS challenge" (Medium)
    This is the main reason /crawl/stream is used instead of /crawl.
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

# Below this length the page is treated as having no real content.
# Calibration: Reuters blocked by DataDome returned raw_markdown of exactly 1
# character, while example.com is a legitimate small page at 165 characters --
# a threshold of 200 misclassified it. 50 separates "essentially nothing" from
# "small but real".
MIN_CONTENT_CHARS = 50

RETRYABLE = {TIMEOUT, FETCH_FAILED, RATE_LIMITED}

_HINTS = {
    BLOCKED_ANTIBOT: (
        "The site blocked this with anti-bot protection. Retrying will not help "
        "(the same page was blocked on 4 consecutive attempts); use a different source."
    ),
    NOT_FOUND: (
        "The page does not exist. The returned content is an error page, not data -- "
        "do not answer from it."
    ),
    HTTP_ERROR: "The server returned an error status; the content retrieved is not trustworthy.",
    UNSUPPORTED_CONTENT: "This file type (PDF or binary) is not supported by this tool.",
    EMPTY_CONTENT: (
        "The content retrieved was empty, which usually means the page requires "
        "JavaScript or was silently blocked."
    ),
    TIMEOUT: "The fetch timed out. Retry at most once.",
    FETCH_FAILED: "Could not reach the upstream crawling service.",
    BLOCKED_URL: "The destination is not permitted. Do not rewrite the URL to get around this.",
    BLOCKED_REDIRECT: "The redirect landed outside the permitted range; the result was discarded.",
    RATE_LIMITED: (
        "Too many requests; the rate limit has been reached. Try again later or "
        "reduce the call frequency."
    ),
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
    """Classify one upstream result row (a single JSON line from /crawl/stream)."""
    err = (result.get("error_message") or "").strip()
    status = result.get("status_code")

    if err:
        low = err.lower()
        if "anti-bot" in low or "antibot" in low:
            # Preserve the protection upstream named (DataDome, PerimeterX,
            # Cloudflare JS challenge).
            detail = err.split(":", 1)[-1].strip() if ":" in err else err
            return Outcome(BLOCKED_ANTIBOT, detail[:160])
        if "download is starting" in low:
            return Outcome(UNSUPPORTED_CONTENT, "upstream tried to download a file instead of rendering")
        if "timeout" in low or "timed out" in low:
            return Outcome(TIMEOUT, err[:160])
        return Outcome(FETCH_FAILED, err[:160])

    if isinstance(status, int):
        if status in (404, 410):
            return Outcome(NOT_FOUND, f"HTTP {status}")
        # 3xx is a normal redirect, gated separately by admission.check_redirect.
        if status >= 400:
            return Outcome(HTTP_ERROR, f"HTTP {status}")

    if not result.get("success", False):
        return Outcome(FETCH_FAILED, "upstream reported success=false")

    if len(markdown.strip()) < MIN_CONTENT_CHARS:
        return Outcome(EMPTY_CONTENT, f"only {len(markdown.strip())} chars")

    return Outcome(OK)
