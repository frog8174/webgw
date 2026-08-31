"""把 markdown 依標題切成章節。

切分依據是 markdown 標題 (#~####)。標題之前的內容歸入 s0「(前言)」。
每節記錄自己的 token 成本 —— 這是讓 agent 判斷「取這節划不划算」的關鍵。

另外會丟掉目錄/索引章節,理由見 is_index_section()。
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

# 低於此 token 數的章節不納入候選 —— 多半是分隔線、單行連結之類的碎片。
MIN_SECTION_TOKENS = 20

# 標題顯示長度上限。只影響輸出,不影響檢索 —— 標題行不在 body 裡,
# 若在此截斷會讓超出的文字永久消失於索引之外。
TITLE_DISPLAY_CHARS = 120

# 目錄判定門檻。實測分離度很寬:目錄的標題覆蓋率 62~90%、連結密度 92~95%,
# 而所有非目錄章節的覆蓋率都 <= 10%。
INDEX_COVERAGE_THRESHOLD = 0.4
INDEX_LINK_RATIO_THRESHOLD = 0.5


def strip_links(text: str) -> str:
    """把 [文字](網址) 縮成 文字。網址對排序和閱讀都是雜訊。"""
    return _LINK.sub(r"\1", text)


def _norm_title(text: str) -> str:
    """正規化標題以便與目錄的連結文字比對。

    要處理三種裝飾:維基的 [编辑]、Sphinx 的 ¶、以及目錄項的前導編號
    (目錄寫「1 背景」而標題是「背景」)。
    """
    text = _DECOR.sub("", text)
    text = _PUNCT.sub("", text)
    return _LEADING_NUM.sub("", text).strip().lower()


def _link_ratio(body: str) -> float:
    n = len(body) or 1
    return sum(len(m.group()) for m in _LINK.finditer(body)) / n


def is_index_section(body: str, normalized_titles: list[str]) -> bool:
    """判定是否為目錄/索引章節。

    目錄包含全頁標題,所以對任何查詢都拿高分,卻不含任何內容 —— 實測它會排在
    真正答案之前(中文維基查「自注意力機制的背景」時,「目录」排第 2,真值排第 5)。

    用結構訊號而非關鍵字黑名單,所以語言無關。兩個條件都要滿足:
      1. 此節的連結文字覆蓋了全頁大部分標題
      2. 此節幾乎全是連結,沒有散文

    只用條件 1 會誤判論文的交叉引用章節 —— 實測 ar5iv 的
    「6.1 Machine Translation」曾被誤判為目錄,而那正是英文測試的 ground truth。
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
        """給人/agent 看的短標題。檢索一律用完整的 self.title。"""
        return self.title[:TITLE_DISPLAY_CHARS]

    def as_outline_entry(self) -> dict:
        return {
            "id": self.id,
            "level": self.level,
            "title": self.display_title,
            "tokens": self.tokens,
        }


def split(markdown: str) -> list[Section]:
    """切分為章節,已濾掉過小的碎片與目錄。順序保留原文件順序。"""
    raw_sections: list[tuple[int, str, list[str]]] = []
    level, title, buf = 0, "(前言)", []

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

    # 丟掉目錄章節。它們沒有內容價值,卻會壓過真正的答案;
    # agent 需要的章節清單由我們自己產生的 outline 提供,不必依賴頁面的目錄。
    titles = [_norm_title(s.title) for s in candidates if s.title != "(前言)"]
    titles = [t for t in titles if len(t) >= 2]
    return [s for s in candidates if not is_index_section(s.body, titles)]
