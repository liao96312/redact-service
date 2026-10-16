"""T1.6 检测入口：运行全部识别器，合并重叠 Span。

重叠规则：保留更长的 Span；长度相同按优先级（词典 > 规则 > 上下文 > NER）。
"""
from __future__ import annotations

from .config import cfg
from .ctx import ctx_spans
from .dict_match import dict_spans
from .ner import ner_spans
from .rules import rule_spans
from .span import Span

_PRIORITY = {
    "dict": int(cfg("priority", "dict", default=4)),
    "rule": int(cfg("priority", "rule", default=3)),
    "ctx": int(cfg("priority", "ctx", default=2)),
    "ner": int(cfg("priority", "ner", default=1)),
}


def _prio(span: Span) -> int:
    return _PRIORITY.get(span.source, 0)


def merge(spans: list[Span]) -> list[Span]:
    """贪心合并：长优先，其次高优先级；去除完全重复。"""
    uniq: dict[tuple[int, int, str], Span] = {}
    for s in spans:
        key = (s.start, s.end, s.type)
        cur = uniq.get(key)
        if cur is None or _prio(s) > _prio(cur):
            uniq[key] = s
    ordered = sorted(
        uniq.values(),
        key=lambda s: (s.length, _prio(s)),
        reverse=True,
    )
    kept: list[Span] = []
    for s in ordered:
        if any(s.overlaps(k) for k in kept):
            continue
        kept.append(s)
    kept.sort(key=lambda s: (s.start, s.end))
    return kept


def detect(text: str, use_ner: bool = True) -> list[Span]:
    if not text:
        return []
    spans: list[Span] = []
    spans += dict_spans(text)
    spans += rule_spans(text)
    spans += ctx_spans(text)
    if use_ner:
        spans += ner_spans(text)

    # 跨行地址：合并被换行拆开的 CJK 文本后再检测一次（仅规则/词典/上下文）
    joined, idx = _join_cjk_lines(text)
    if joined != text:
        for s in dict_spans(joined) + rule_spans(joined) + ctx_spans(joined):
            if s.start >= len(idx) or s.end - 1 >= len(idx):
                continue
            spans.append(Span(idx[s.start], idx[s.end - 1] + 1, s.type, s.score, s.source))
    return merge(spans)


def _is_cjk(ch: str) -> bool:
    return (
        "\u4e00" <= ch <= "\u9fa5"
        or "\u3400" <= ch <= "\u4dbf"
        or ch in "，。、；：（）"
    )


def _join_cjk_lines(text: str) -> tuple[str, list[int]]:
    """若换行两侧都是中日韩字符，则视为软换行并合并，返回合并文本及原索引映射。"""
    chars: list[str] = []
    idx: list[int] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "\n" and 0 < i < n - 1 and _is_cjk(text[i - 1]) and _is_cjk(text[i + 1]):
            i += 1
            continue
        chars.append(text[i])
        idx.append(i)
        i += 1
    return "".join(chars), idx


def detect_text_only(text: str) -> list[Span]:
    """不调用 NER 的轻量检测（用于 verify 早期判断等场景）。"""
    return detect(text, use_ner=False)
