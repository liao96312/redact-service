"""T1.6 检测入口：运行全部识别器，合并重叠 Span。

重叠规则：保留更长的 Span；长度相同按优先级（词典 > 规则 > 上下文 > NER）。

真实图纸语料上的修正：
- NER 在带尺寸数字/表格的图纸文本上噪声极大（会把 "0\\n8" 判成人名），
  因此统一做「实体合理性过滤」：含换行、纯数字、过短、纯标点的 NER 结果丢弃；
- 跨行合并只用于 CJK 软换行，避免把表格列拼成假实体。
"""
from __future__ import annotations

import re

from .config import cfg
from .ctx import ctx_spans
from .dict_match import dict_spans
from .ner import ner_spans
from .rules import path_spans, rule_spans
from .span import Span

_PRIORITY = {
    "dict": int(cfg("priority", "dict", default=4)),
    "rule": int(cfg("priority", "rule", default=3)),
    "ctx": int(cfg("priority", "ctx", default=2)),
    "ner": int(cfg("priority", "ner", default=1)),
}

# NER 结果的最低长度（按类型）
_NER_MIN_LEN = {"PERSON": 2, "ORG": 3, "ADDRESS": 4, "PHONE": 6}

# 图纸/聊天截图里被 NER 误当实体的高频噪声词（全大写缩写、界面词、时间词）
_NER_STOPWORDS = {
    "pdf", "jpg", "jpeg", "png", "excel", "word", "ppt", "pptx", "docx", "xlsx",
    "tel", "fax", "attn", "dtc", "hp", "mp", "no", "qty", "pcs", "sku", "pi", "ci",
    "pl", "spec", "size", "color", "colour", "item", "model", "total", "amount",
    "china", "chinese", "english", "yes", "ok", "dear", "thanks", "please",
    "上午", "下午", "下午好", "上午好", "今天", "明天", "昨天", "星期", "周五", "周一",
    "您", "你好", "谢谢", "收到", "确认", "已读", "未读",
}

# 地址必须像地址：含行政区划或街道后缀
_ADDR_SHAPE = re.compile(
    r"(省|市|区|县|镇|村|路|街|道|巷|弄|号|大厦|大楼|工业园|花园|小区|"
    r"Street|Road|Rd\.?|Avenue|Ave\.?|Lane|Blvd|Drive|Zone|District|City|Province)",
    re.IGNORECASE,
)
# 英文地址片段：地名/街道/国家这类词，NER 常误判成 PERSON/ORG
_EN_ADDR_WORDS = re.compile(
    r"\b(?:"
    r"Rd|Road|St|Street|Ave|Avenue|Blvd|Lane|Ln|Drive|Dr|Way|Zone|District|City|"
    r"Province|Industrial|Area|Center|Centre|Building|Bld|Floor|Foshan|Guangdong|"
    r"Nanhai|Heshun|Hegui|Lvjing|Meiju|China|Indonesia|Banten|Tangerang|Jakarta|"
    r"Singapore|Malaysia|Vietnam|Thailand|India|Dubai|Sydney|Melbourne|Brisbane"
    r")\b",
    re.IGNORECASE,
)
# 时间/日期/尺寸这类含数字的片段不可能是人名或公司
_TIME_LIKE = re.compile(r"\d{1,2}\s*[:：]\s*\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")


def _prio(span: Span) -> int:
    return _PRIORITY.get(span.source, 0)


def _ner_acceptable(text: str, span: Span) -> bool:
    """NER 结果的合理性过滤。

    真实图纸文本里 NER 会把尺寸数字、界面按钮、时间戳判成实体，直接采信会
    大面积误涂（把图面文字涂黑），因此这里按类型做结构约束：
    - PERSON：不含数字/标点噪声，2~20 字；
    - ORG：不含数字，至少 3 字，排除全大写缩写与界面词；
    - ADDRESS：必须含行政区划或街道后缀；
    - PHONE：必须像号码。
    """
    frag = text[span.start:span.end]
    if not frag or frag != frag.strip():
        return False
    if any(ch in frag for ch in "\n\r\t"):
        return False
    if span.length < _NER_MIN_LEN.get(span.type, 2):
        return False
    if frag.strip().lower() in _NER_STOPWORDS:
        return False
    # 纯数字/纯符号/含小数点数字串（尺寸标注）一律不要
    if re.fullmatch(r"[\d\s.,:：()（）/\\\-–—*#%°\"'′″×xX+~]+", frag):
        return False
    if re.search(r"\d{1,}[\s.]*\d*\.\d", frag):       # 12.5 / 1.2.3 尺寸与编号
        return False
    if sum(ch.isdigit() for ch in frag) / len(frag) > 0.5:
        return False
    if _TIME_LIKE.search(frag):
        return False

    han = sum(1 for ch in frag if "\u4e00" <= ch <= "\u9fa5")
    alpha = sum(1 for ch in frag if ch.isascii() and ch.isalpha())

    # 英文地名/街道片段（Lvjing Rd / Foshan / Hegui Industrial Zone）既不是人名
    # 也不是公司，NER 却经常这么判；先按「像地址」处理掉。
    if span.type in ("PERSON", "ORG") and alpha > han and _EN_ADDR_WORDS.search(frag):
        return False

    if span.type == "PERSON":
        if any(ch.isdigit() for ch in frag):
            return False
        # 只允许字母、汉字、以及人名里常见的连接符
        if re.search(r"[^\w\u4e00-\u9fa5·\-'’. ]", frag):
            return False
        # 纯英文「词 + 词」形式，且含地名/机构通用词时不可信
        return han + alpha >= 2 and span.length <= 20

    if span.type == "ORG":
        if any(ch.isdigit() for ch in frag):
            return False
        letters = [ch for ch in frag if ch.isascii() and ch.isalpha()]
        if letters and frag.strip().isupper() and len(frag.strip()) <= 5:
            return False          # PDF / TEL / DTC 这类全大写缩写
        return han + alpha >= 3

    if span.type == "ADDRESS":
        return bool(_ADDR_SHAPE.search(frag)) and han + alpha >= 3

    if span.type == "PHONE":
        return bool(re.fullmatch(r"\+?[\d][\d\s\-()]{5,24}", frag))

    return han + alpha >= 2


def merge(spans: list[Span]) -> list[Span]:
    """贪心合并：长优先，其次高优先级；去除完全重复。"""
    uniq: dict[tuple[int, int, str], Span] = {}
    for s in spans:
        key = (s.start, s.end, s.type)
        cur = uniq.get(key)
        if cur is None or _prio(s) > _prio(cur):
            uniq[key] = s
    # 先按 (长度, 优先级) 排序：长实体先占位，短的重叠碎片被丢弃
    ordered = sorted(
        uniq.values(),
        key=lambda s: (s.length, _prio(s), s.score),
        reverse=True,
    )
    kept: list[Span] = []
    for s in ordered:
        if any(s.overlaps(k) for k in kept):
            continue
        kept.append(s)
    kept.sort(key=lambda s: (s.start, s.end))
    return kept


def _join_cjk_lines(text: str) -> tuple[str, list[int]]:
    """把被软换行拆开的 CJK 文本合并，返回合并文本及原索引映射。

    修正：只在「明显是同一句话被折断」时合并，即换行左侧不是标点/标签结束符、
    右侧不是字段标签开头。旧实现无条件合并所有 CJK 之间的换行，
    结果「户名：李四\n电话：138…」被拼成「户名：李四电话：138…」，
    人名正则于是把「李四电话」当成一个人名，把「电话」标签涂掉了。
    """
    chars: list[str] = []
    idx: list[int] = []
    i, n = 0, len(text)
    while i < n:
        if (
            text[i] == "\n"
            and 0 < i < n - 1
            and _is_cjk(text[i - 1])
            and _is_cjk(text[i + 1])
            and not _next_is_label(text, i + 1)
            and text[i - 1] not in "。！？；：，、"
        ):
            i += 1
            continue
        chars.append(text[i])
        idx.append(i)
        i += 1
    return "".join(chars), idx


# 常见字段标签开头：换行后紧跟这些词，说明是新字段，不能与上一行拼接
_LABEL_HEADS = (
    "电话", "手机", "姓名", "户名", "地址", "公司", "单位", "邮箱", "传真",
    "联系人", "负责人", "经办人", "开户行", "卡号", "账号", "日期", "编号",
    "项目", "部门", "职位", "备注", "产品", "数量", "单价", "金额", "合计",
)


def _next_is_label(text: str, pos: int) -> bool:
    head = text[pos:pos + 4]
    return any(head.startswith(lb) for lb in _LABEL_HEADS)


def _is_cjk(ch: str) -> bool:
    return (
        "\u4e00" <= ch <= "\u9fa5"
        or "\u3400" <= ch <= "\u4dbf"
        or ch in "，。、；：（）"
    )


# 低于该长度的文本不跑 NER：
# 1) 短文本（表头、色号、规格）里根本没有可识别的实体，NER 只会输出噪声；
# 2) HanLP 分词器对 2~8 字输入会走「超长文本」保护分支，实测单次 3 秒，
#    一份 3494 个字符串的订单表因此耗时 379 秒直接超时。
_NER_MIN_CHARS = int(cfg("limits", "ner_min_chars", default=12))


def detect(text: str, use_ner: bool = True) -> list[Span]:
    if not text:
        return []
    spans: list[Span] = []
    spans += dict_spans(text)
    spans += rule_spans(text)
    spans += ctx_spans(text)
    if use_ner and len(text) >= _NER_MIN_CHARS:
        spans += [s for s in ner_spans(text) if _ner_acceptable(text, s)]

    # 跨行地址：合并被换行拆开的 CJK 文本后再检测一次（仅规则/词典/上下文）
    joined, idx = _join_cjk_lines(text)
    if joined != text:
        for s in dict_spans(joined) + rule_spans(joined) + ctx_spans(joined):
            if s.start >= len(idx) or s.end - 1 >= len(idx):
                continue
            spans.append(Span(idx[s.start], idx[s.end - 1] + 1, s.type, s.score, s.source))
    return merge(spans)


def detect_text_only(text: str) -> list[Span]:
    """不调用 NER 的轻量检测（用于 verify 早期判断等场景）。"""
    return detect(text, use_ner=False)


def detect_paths(text: str) -> list[Span]:
    """只识别网盘/本地路径（用于正文里的客户目录路径）。"""
    return merge(path_spans(text)) if text else []
