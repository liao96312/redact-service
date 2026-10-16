"""T1.3 上下文关键词识别。只取第 1 个捕获组作为 Span。"""
from __future__ import annotations

import re

from .config import cfg
from .span import Span

_CTX_SCORE = float(cfg("score", "ctx", default=0.85))

CTX = [
    ("PERSON", r"(?:户名|姓名|联系人|客户|收款人|法人|负责人|经办人)\s*[:：]\s*([\u4e00-\u9fa5·]{2,6})"),
    ("ORG", r"(?:公司名称|单位名称|甲方|乙方|开户行)\s*[:：]\s*([^\n，,;；]{4,40})"),
    ("ADDRESS", r"(?:地址|住所|注册地址|办公地址)\s*[:：]\s*([^\n;；]{6,60})"),
    ("PERSON", r"(?:Name|Customer|Client|Beneficiary|Account Holder)\s*[:：]\s*([A-Z][A-Za-z.\-]*(?: [A-Z][A-Za-z.\-]*){0,3})"),
    ("ORG", r"(?:Company|Corporation|Organization|Employer|Vendor|Supplier)\s*[:：]\s*([^\n，,;；]{2,40})"),
    ("ADDRESS", r"Address\s*[:：]\s*([^\n]{8,80})"),
]

_COMPILED = [(t, re.compile(p)) for t, p in CTX]


def ctx_spans(text: str) -> list[Span]:
    if not text:
        return []
    out: list[Span] = []
    for typ, rex in _COMPILED:
        for m in rex.finditer(text):
            if m.group(1):
                s, e = m.span(1)
                out.append(Span(s, e, typ, _CTX_SCORE, "ctx"))
    return out
