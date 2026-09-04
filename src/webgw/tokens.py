"""Token counting.

Uses tiktoken's cl100k_base as a budget proxy. Local models (Qwen, Llama) ship
different tokenizers, so this is an approximation rather than an exact count --
good enough for deciding how much text fits in a selection budget, but do not
treat it as precise billing. The encoding file is pre-cached at image build
time, so no outbound network access is needed at runtime.
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
    """Cut to the given token count, returning a fully decodable string."""
    if max_tokens <= 0:
        return ""
    enc = _encoder()
    ids = enc.encode(text)
    if len(ids) <= max_tokens:
        return text
    return enc.decode(ids[:max_tokens])
