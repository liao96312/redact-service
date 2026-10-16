"""T1.2 规则识别（正则 + 校验）。

规则以起点实现，后续以测试结果调优。所有 Span 的 source 固定为 "rule"。
"""
from __future__ import annotations

import re

from .config import cfg
from .span import (
    ADDRESS,
    BANK_CARD,
    EMAIL,
    ID_CARD,
    ORG,
    PERSON,
    PHONE,
    Span,
)

_RULE_SCORE = float(cfg("score", "rule", default=0.9))

PHONE_PATTERNS = [
    r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d[- ]?\d{4}[- ]?\d{4}(?!\d)",
    r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)",
    r"(?<!\d)[48]00[- ]?\d{3}[- ]?\d{4}(?!\d)",
    r"(?<!\w)\+?\d{1,3}[ -]?\(?\d{2,4}\)?[ -]\d{3,4}[ -]\d{3,4}(?!\d)",
]
CARD = r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"
ID18 = r"(?<!\d)\d{17}[\dXx](?!\d)"
EMAIL_PATTERN = r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
ORG_ZH = r"[\u4e00-\u9fa5（）()]{2,25}?(?:股份有限公司|有限责任公司|有限公司|集团|分公司|事务所|研究院|支行)"
ORG_EN = r"(?:[A-Z][\w&.\-]*\s+){1,5}(?:Inc\.?|LLC|Ltd\.?|Limited|Corp\.?|Corporation|GmbH|PLC)"
ADDR_ZH = r"(?:[\u4e00-\u9fa5]{2,9}(?:省|自治区))?[\u4e00-\u9fa5]{2,9}市[\u4e00-\u9fa5]{1,9}(?:区|县)[\u4e00-\u9fa5A-Za-z0-9\-]{1,40}?(?:路|街|道|巷|弄|大道)[\dA-Za-z\-]*号?[\u4e00-\u9fa5A-Za-z0-9\-]{0,30}"
ADDR_EN = r"\d{1,5}\s+[A-Z][A-Za-z0-9.\s]{2,40}\s(?:Street|St\.?|Road|Rd\.?|Avenue|Ave\.?|Blvd|Lane|Ln\.?|Drive|Dr\.?)(?:,\s*[A-Za-z\s]+){0,3}"

_PHONE_RE = re.compile("|".join(f"(?:{p})" for p in PHONE_PATTERNS))
_CARD_RE = re.compile(CARD)
_ID18_RE = re.compile(ID18)
_EMAIL_RE = re.compile(EMAIL_PATTERN)
_ORG_ZH_RE = re.compile(ORG_ZH)
_ORG_EN_RE = re.compile(ORG_EN)
_ADDR_ZH_RE = re.compile(ADDR_ZH)
_ADDR_EN_RE = re.compile(ADDR_EN)

# 银行卡/账号 前置关键词（匹配前 10 字符内出现即可）
_ACCT_KW = re.compile(r"(卡号|账号|帐号|account|card)", re.IGNORECASE)


def luhn_ok(num: str) -> bool:
    s = 0
    for i, ch in enumerate(reversed(num)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        s += d
    return s % 10 == 0


_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


def id18_ok(num: str) -> bool:
    if not re.fullmatch(r"\d{17}[\dXx]", num):
        return False
    total = sum(int(num[i]) * _ID_WEIGHTS[i] for i in range(17))
    return _ID_CHECK[total % 11] == num[17].upper()


def _phone_spans(text: str) -> list[Span]:
    out = []
    for m in _PHONE_RE.finditer(text):
        out.append(Span(m.start(), m.end(), PHONE, _RULE_SCORE, "rule"))
    return out


def _card_spans(text: str) -> list[Span]:
    out = []
    for m in _CARD_RE.finditer(text):
        raw = m.group(0)
        num = re.sub(r"[ -]", "", raw)
        if not (16 <= len(num) <= 19):
            continue
        # 18 位且身份证校验通过时，交给 ID_CARD 规则，避免误判为银行卡
        if len(num) == 18 and id18_ok(num):
            continue
        pref = text[max(0, m.start() - 10):m.start()]
        # 去掉末尾多余的分隔符（正则允许 trailing 分隔）
        end = m.end()
        while end > m.start() and text[end - 1] in " -":
            end -= 1
        if luhn_ok(num) or _ACCT_KW.search(pref):
            out.append(Span(m.start(), end, BANK_CARD, _RULE_SCORE, "rule"))
    return out


def _id_spans(text: str) -> list[Span]:
    out = []
    for m in _ID18_RE.finditer(text):
        if id18_ok(m.group(0)):
            out.append(Span(m.start(), m.end(), ID_CARD, _RULE_SCORE, "rule"))
    return out


def _email_spans(text: str) -> list[Span]:
    return [Span(m.start(), m.end(), EMAIL, _RULE_SCORE, "rule") for m in _EMAIL_RE.finditer(text)]


def _org_spans(text: str) -> list[Span]:
    out = []
    for rex in (_ORG_ZH_RE, _ORG_EN_RE):
        for m in rex.finditer(text):
            out.append(Span(m.start(), m.end(), ORG, _RULE_SCORE, "rule"))
    return out


def _addr_spans(text: str) -> list[Span]:
    out = []
    for rex in (_ADDR_ZH_RE, _ADDR_EN_RE):
        for m in rex.finditer(text):
            out.append(Span(m.start(), m.end(), ADDRESS, _RULE_SCORE, "rule"))
    return out


def rule_spans(text: str) -> list[Span]:
    if not text:
        return []
    spans: list[Span] = []
    spans += _phone_spans(text)
    spans += _card_spans(text)
    spans += _id_spans(text)
    spans += _email_spans(text)
    spans += _org_spans(text)
    spans += _addr_spans(text)
    return spans
