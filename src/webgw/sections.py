"""Split markdown into sections by heading.

Sections are cut at markdown headings (# through ####). Anything before the
first heading becomes s0, "(preamble)". Each section records its own token
cost -- that is what lets an agent judge whether pulling a section is worth it.

Table-of-contents sections are dropped; see is_index_section() for why.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import tokens

_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*$")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_DECOR = re.compile(r"\[(编辑|編輯|edit)\]", re.I)
_PUNCT = re.compile(r"[¶#*\s]+")
_LEADING_NUM = re.compile(r"^[\d.\s]+")

PREAMBLE_TITLE = "(preamble)"

# Sections below this token count are not considered -- they are mostly
# fragments such as horizontal rules or single-line links.
MIN_SECTION_TOKENS = 20

# Display cap for titles. Output only, never retrieval: the heading line is not
# part of the body, so truncating it here would permanently hide the overflow
# from the index.
TITLE_DISPLAY_CHARS = 120

# Table-of-contents thresholds. Separation measured wide: a real TOC covers
# 62-90% of page titles at 92-95% link density, while every non-TOC section
# measured <= 10% coverage.
INDEX_COVERAGE_THRESHOLD = 0.4
INDEX_LINK_RATIO_THRESHOLD = 0.5


def strip_links(text: str) -> str:
    """Reduce [text](url) to text. URLs are noise for both ranking and reading."""
    return _LINK.sub(r"\1", text)


def _norm_title(text: str) -> str:
    """Normalize a title so it can be matched against TOC link text.

    Three kinds of decoration have to go: Wikipedia's [edit], Sphinx's paragraph
    mark, and the leading numbers TOC entries carry (the TOC says "1 Background"
    where the heading says "Background").
    """
    text = _DECOR.sub("", text)
    text = _PUNCT.sub("", text)
    return _LEADING_NUM.sub("", text).strip().lower()


def _link_ratio(body: str) -> float:
    n = len(body) or 1
    return sum(len(m.group()) for m in _LINK.finditer(body)) / n


def is_index_section(body: str, normalized_titles: list[str]) -> bool:
    """Detect a table-of-contents / index section.

    A TOC contains every heading on the page, so it scores highly against any
    query while containing no actual content. It was measured outranking the
    real answer: on Chinese Wikipedia, querying for the background of
    self-attention put the TOC 2nd and the true section 5th.

    Detection uses structural signals rather than a keyword blocklist, so it is
    language-independent. Both conditions must hold:
      1. this section's link text covers most of the page's headings
      2. this section is almost entirely links, with no prose

    Condition 1 alone misfires on cross-reference sections in papers -- the
    ar5iv section "6.1 Machine Translation" was misclassified as a TOC, and
    that was the ground truth for an English test case.
    """
    if _link_ratio(body) < INDEX_LINK_RATIO_THRESHOLD:
        return False
    anchors = {_norm_title(a) for a in _LINK.findall(body)}
    anchors = {a for a in anchors if len(a) >= 2}
    if not anchors or not normalized_titles:
        return False
    covered = sum(1 for t in normalized_titles if t in anchors)
    return covered / len(normalized_titles) >= INDEX_COVERAGE_THRESHOLD


@dataclass
class Section:
    id: str
    level: int
    title: str
    body: str
    position: int
    tokens: int = field(default=0)

    @property
    def display_title(self) -> str:
        """Short title for humans and agents. Retrieval always uses self.title."""
        return self.title[:TITLE_DISPLAY_CHARS]

    def as_outline_entry(self) -> dict:
        return {
            "id": self.id,
            "level": self.level,
            "title": self.display_title,
            "tokens": self.tokens,
        }


def split(markdown: str) -> list[Section]:
    """Split into sections, dropping tiny fragments and the TOC.

    Document order is preserved.
    """
    raw_sections: list[tuple[int, str, list[str]]] = []
    level, title, buf = 0, PREAMBLE_TITLE, []

    for line in markdown.splitlines():
        m = _HEADING.match(line)
        if m:
            if buf:
                raw_sections.append((level, title, buf))
            level = len(m.group(1))
            title = strip_links(m.group(2)).strip()
            buf = []
        else:
            buf.append(line)
    if buf:
        raw_sections.append((level, title, buf))

    candidates: list[Section] = []
    for idx, (lv, ti, lines) in enumerate(raw_sections):
        body = "\n".join(lines).strip()
        sec = Section(
            id=f"s{idx}", level=lv, title=ti, body=body, position=idx, tokens=tokens.count(body)
        )
        if sec.tokens >= MIN_SECTION_TOKENS:
            candidates.append(sec)

    # Drop TOC sections. They carry no content value but outrank real answers,
    # and the agent gets its section list from the outline we build ourselves,
    # so nothing depends on the page's own TOC.
    titles = [_norm_title(s.title) for s in candidates if s.title != PREAMBLE_TITLE]
    titles = [t for t in titles if len(t) >= 2]
    return [s for s in candidates if not is_index_section(s.body, titles)]
