"""T1.3 上下文关键词识别。只取第 1 个捕获组作为 Span。

真实图纸里高频出现的中英文「标签: 值」形态都在这里集中声明，
包括工作证表格里的「姓名 / 电话 / 职位」上下行结构。
"""
from __future__ import annotations

import re

from .config import cfg
from .span import ADDRESS, BANK_CARD, EMAIL, ORG, PERSON, PHONE, Span

_CTX_SCORE = float(cfg("score", "ctx", default=0.85))

CTX = [
    # ---- 中文标签
    # 人名值：2~6 个汉字/间隔号，且不能跨行、不能把下一个标签（电话/地址…）吞进来。
    # 旧写法 `[\u4e00-\u9fa5·]{2,6}` 会把「李四\n电话」里的「电话」也算进人名，
    # 因为 `\n` 之后紧跟汉字，长度正好没超限。
    ("PERSON", r"(?:户名|姓名|联系人|客户|客户姓名|收款人|法人|法人代表|负责人|经办人|业务员|跟单员|下单人|收货人|联系人姓名)"
               r"\s*[:：]\s*([\u4e00-\u9fa5·]{2,6})(?![\u4e00-\u9fa5])"),
    ("ORG", r"(?:公司名称|单位名称|公司|甲方|乙方|开户行|开户银行|供应商|抬头)\s*[:：]\s*([^\n，,;；]{4,40})"),
    ("ADDRESS", r"(?:地址|住所|注册地址|办公地址|收货地址|送货地址|工厂地址|安装地址)\s*[:：]\s*([^\n;；]{6,80})"),
    ("PHONE", r"(?:电话|手机|手机号|联系电话|联系方式|座机|传真|Tel|Mobile|Phone|Contact)\s*[:：]\s*(\+?[\d][\d\s\-()]{6,20})"),
    ("BANK_CARD", r"(?:卡号|账号|帐号|银行账号|Account\s*(?:No\.?|Number)?)\s*[:：]\s*([\d][\d\s\-]{10,30})"),
    ("EMAIL", r"(?:邮箱|电子邮箱|邮件|E-?mail)\s*[:：]\s*([\w.+-]+@[\w-]+(?:\.[\w-]+)+)"),
    # ---- 英文标签
    # 英文值只取「像人名/像公司名」的形态：首字母大写，不含 & + / 等连接符，
    # 也不跨越项目编号（"Project Name: Princeton APT & SOHO -P1" 只应命中
    # "Princeton APT"）。
    ("PERSON", r"(?:Name|Customer|Customer Name|Client|Beneficiary|Account Holder|Contact(?: Person)?|Attn)\s*[:：]\s*([A-Z][A-Za-z.\-'’]*(?: [A-Z][A-Za-z.\-'’]*){0,3})"),
    ("ORG", r"(?:Company|Corporation|Organization|Employer|Vendor|Supplier)\s*[:：]\s*([^\n，,;；&]{2,40})"),
    ("ADDRESS", r"(?:Address|Ship To|Bill To|Delivery Address|Site)\s*[:：]\s*([^\n]{8,80})"),
    ("PHONE", r"(?:Tel|Telephone|Phone|Mobile|Contact No\.?|HP)\s*[:：]\s*(\+?[\d][\d\s\-()]{6,20})"),
    ("BANK_CARD", r"(?:Account No\.?|Account Number|A/C No\.?|Card No\.?)\s*[:：]\s*([\d][\d\s\-]{10,30})"),
]

# 竖排表格：标签与值分处相邻两行（工作证/下单页脚常见）
VERTICAL_LABELS = {
    "姓名": PERSON,
    "名字": PERSON,
    "户名": PERSON,
    "电话": PHONE,
    "手机": PHONE,
    "手机号": PHONE,
    "联系方式": PHONE,
    "职位": None,
    "部门名称": None,
    "邮箱": EMAIL,
    "地址": ADDRESS,
    "客户名称": PERSON,
    "客户": PERSON,
}

_COMPILED = [(t, re.compile(p)) for t, p in CTX]


def _vertical_spans(text: str) -> list[Span]:
    """处理「标签一行、值下一行」的竖排表格；同一标签下连续多行值都算命中。"""
    out: list[Span] = []
    lines = text.split("\n")
    if len(lines) < 2:
        return out

    offsets: list[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1

    for i, raw in enumerate(lines[:-1]):
        label = raw.strip().rstrip(":：")
        typ = VERTICAL_LABELS.get(label)
        if typ is None:
            continue
        # 向下取 1~4 行作为候选值，遇到空行或下一个标签即停
        for j in range(i + 1, min(i + 5, len(lines))):
            val = lines[j].strip()
            if not val:
                break
            if val.rstrip(":：") in VERTICAL_LABELS:
                break
            if typ == PHONE:
                if not re.fullmatch(r"\+?[\d][\d\s\-()]{5,24}", val):
                    continue
            elif typ == EMAIL:
                if not re.fullmatch(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", val):
                    continue
            elif typ == PERSON:
                if not re.fullmatch(r"[\u4e00-\u9fa5·]{2,6}|[A-Z][A-Za-z.\-'’]*(?: [A-Z][A-Za-z.\-'’]*){0,3}", val):
                    continue
            else:
                if len(val) < 4:
                    continue
            start = offsets[j] + (len(lines[j]) - len(lines[j].lstrip()))
            out.append(Span(start, start + len(val), typ, _CTX_SCORE, "ctx"))
    return out


def ctx_spans(text: str) -> list[Span]:
    if not text:
        return []
    out: list[Span] = []
    for typ, rex in _COMPILED:
        for m in rex.finditer(text):
            if m.group(1):
                s, e = m.span(1)
                frag = text[s:e].rstrip()
                if not frag:
                    continue
                out.append(Span(s, s + len(frag), typ, _CTX_SCORE, "ctx"))
    out.extend(_vertical_spans(text))
    return out
