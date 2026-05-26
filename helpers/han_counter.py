"""汉字计数与按汉字数截断。

发言长度只统计汉字(CJK 统一表意文字),emoji/英文/标点/空格不计入。
硬上限见 constants.MAX_HAN_CHARS。
"""

from __future__ import annotations

import re

# CJK 统一表意 + 扩展A + 兼容表意,覆盖常用及部分生僻字
_HAN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
# 优先在这些标点处截断,保留语义完整
_SENTENCE_END = "。！？；，!?,;"


def count_han(text: str) -> int:
    """统计字符串中的汉字数量。"""
    return len(_HAN_RE.findall(text))


def truncate_by_han(text: str, max_han: int) -> str:
    """把文本截断到至多 ``max_han`` 个汉字。

    优先在标点处截断;若截断点之前无标点则按 codepoint 硬截到
    第 max_han 个汉字所在位置。
    """
    if count_han(text) <= max_han:
        return text

    # 逐字符累计汉字数,记录达到上限时的下标
    han_seen = 0
    cut_index = len(text)
    for i, ch in enumerate(text):
        if _HAN_RE.match(ch):
            han_seen += 1
            if han_seen >= max_han:
                cut_index = i + 1
                break

    head = text[:cut_index]
    # 在 head 内寻找最后一个句末标点,优先在此截断
    last_punct = max((head.rfind(p) for p in _SENTENCE_END), default=-1)
    if last_punct > 0:
        return head[: last_punct + 1]
    return head
