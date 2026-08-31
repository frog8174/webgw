"""Token 計數。

用 tiktoken cl100k_base 當預算代理。地端模型(Qwen/Llama)的 tokenizer 不同,
所以這是近似值而非精確值 —— 對「選節預算」這個用途足夠,不要當成精確計費。
容器建置時已預先快取編碼檔,執行期不需連外。
"""
from __future__ import annotations

import functools


@functools.lru_cache(maxsize=1)
def _encoder():
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def count(text: str | None) -> int:
    if not text:
        return 0
    return len(_encoder().encode(text))


def truncate(text: str, max_tokens: int) -> str:
    """截到指定 token 數。回傳的是可解碼的完整字串。"""
    if max_tokens <= 0:
        return ""
    enc = _encoder()
    ids = enc.encode(text)
    if len(ids) <= max_tokens:
        return text
    return enc.decode(ids[:max_tokens])
