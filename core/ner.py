"""T1.5 NER（HanLP 中文 / spaCy 英文）。模型仅在启动时加载一次。

路由：汉字占比 > 阈值 -> HanLP(MSRA NER)；否则 spaCy(en_core_web_sm)。
NER 得分统一 0.6（低于规则与词典）。
"""
from __future__ import annotations

import threading

from .config import cfg
from .span import ADDRESS, ORG, PERSON, PHONE, Span

_NER_SCORE = float(cfg("score", "ner", default=0.6))
_HAN_RATIO = float(cfg("limits", "min_han_char_ratio", default=0.30))
_SPACY_MODEL = cfg("ner", "spacy_model", default="en_core_web_sm")

_lock = threading.Lock()
_loaded = False
_han_tok = None
_han_ner = None
_nlp = None

_HAN_LABEL_MAP = {
    "PERSON": PERSON,
    "ORGANIZATION": ORG,
    "ORG": ORG,
    "LOCATION": ADDRESS,
    "GPE": ADDRESS,
    "PHONE": PHONE,
}
_SPACY_LABEL_MAP = {
    "PERSON": PERSON,
    "ORG": ORG,
    "GPE": ADDRESS,
    "LOC": ADDRESS,
    "FAC": ADDRESS,
}


def _chinese_ratio(text: str) -> float:
    if not text:
        return 0.0
    han = sum(1 for c in text if "\u4e00" <= c <= "\u9fa5")
    return han / len(text)


def load_models() -> None:
    """显式预热；服务/worker 启动时调用一次。失败不影响规则与词典。"""
    global _loaded, _han_tok, _han_ner, _nlp
    with _lock:
        if _loaded:
            return
        try:
            import hanlp

            _han_tok = hanlp.load(hanlp.pretrained.tok.COARSE_ELECTRA_SMALL_ZH)
            _han_ner = hanlp.load(hanlp.pretrained.ner.MSRA_NER_ELECTRA_SMALL_ZH)
        except Exception:
            _han_tok = _han_ner = None
        try:
            import spacy

            _nlp = spacy.load(_SPACY_MODEL)
        except Exception:
            _nlp = None
        _loaded = True


def _hanlp_spans(text: str) -> list[Span]:
    if _han_tok is None or _han_ner is None:
        return []
    tokens = _han_tok(text)
    # 计算 token 到字符偏移
    starts, ends = [], []
    pos = 0
    for t in tokens:
        starts.append(pos)
        pos += len(t)
        ends.append(pos)
    out: list[Span] = []
    for item in _han_ner(tokens):
        word, label, ts, te = item
        if ts >= len(starts) or te - 1 >= len(ends):
            continue
        typ = _HAN_LABEL_MAP.get(label)
        if not typ:
            continue
        out.append(Span(starts[ts], ends[te - 1], typ, _NER_SCORE, "ner"))
    return out


def _spacy_spans(text: str) -> list[Span]:
    if _nlp is None:
        return []
    doc = _nlp(text)
    out: list[Span] = []
    for ent in doc.ents:
        typ = _SPACY_LABEL_MAP.get(ent.label_)
        if not typ:
            continue
        out.append(Span(ent.start_char, ent.end_char, typ, _NER_SCORE, "ner"))
    return out


def ner_spans(text: str) -> list[Span]:
    if not text or not text.strip():
        return []
    load_models()
    if _chinese_ratio(text) > _HAN_RATIO:
        return _hanlp_spans(text)
    return _spacy_spans(text)
