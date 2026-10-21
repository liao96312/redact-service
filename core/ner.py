"""T1.5 NER（HanLP 中文 / spaCy 英文）。模型仅在启动时加载一次。

路由：汉字占比 > 阈值 -> HanLP(MSRA NER)；否则 spaCy(en_core_web_sm)。
NER 得分统一 0.6（低于规则与词典）。

修正点：
- HanLP 分词会吃掉空格/标点，旧代码用「token 长度累加」推算偏移，
  遇到任何空白或全角标点就会整体错位（把标签判成实体）。改为在原文中
  按 token 顺序搜索定位，跳过空白与标点；
- 记录加载失败原因，便于 /healthz 暴露给运维（不再静默降级）；
- 长文本分段推理，避免图纸文本一次喂给 transformer 触发显存/长度上限。
"""
from __future__ import annotations

import threading

from .config import cfg
from .span import ADDRESS, ORG, PERSON, PHONE, Span

_NER_SCORE = float(cfg("score", "ner", default=0.6))
_HAN_RATIO = float(cfg("limits", "min_han_char_ratio", default=0.30))
_SPACY_MODEL = cfg("ner", "spacy_model", default="en_core_web_sm")
_SEGMENT = int(cfg("limits", "ner_segment_chars", default=800))

_lock = threading.Lock()
_loaded = False
_han_tok = None
_han_ner = None
_nlp = None
load_errors: dict[str, str] = {}

_HAN_LABEL_MAP = {
    "PERSON": PERSON,
    "NR": PERSON,
    "ORGANIZATION": ORG,
    "ORG": ORG,
    "NT": ORG,
    "LOCATION": ADDRESS,
    "LOC": ADDRESS,
    "NS": ADDRESS,
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
        except Exception as e:  # noqa: BLE001
            _han_tok = _han_ner = None
            load_errors["hanlp"] = f"{type(e).__name__}: {e}"
        try:
            import spacy

            _nlp = spacy.load(_SPACY_MODEL)
        except Exception as e:  # noqa: BLE001
            _nlp = None
            load_errors["spacy"] = f"{type(e).__name__}: {e}"
        _loaded = True


def ner_status() -> dict:
    return {
        "loaded": _loaded,
        "hanlp": _han_ner is not None,
        "spacy": _nlp is not None,
        "errors": dict(load_errors),
    }


def _token_offsets(text: str, tokens: list[str]) -> list[tuple[int, int]]:
    """在原文中按顺序定位每个 token，跳过被分词器丢掉的空白与标点。"""
    offsets: list[tuple[int, int]] = []
    pos = 0
    n = len(text)
    for tok in tokens:
        tok = str(tok)
        if not tok:
            offsets.append((pos, pos))
            continue
        i = text.find(tok, pos)
        if i < 0:
            # 分词器做了归一化（全角/半角、大小写），退化：按长度前进
            i = min(pos, max(0, n))
            j = min(i + len(tok), n)
        else:
            j = i + len(tok)
        offsets.append((i, j))
        pos = j
    return offsets


def _chunks(text: str) -> list[tuple[int, str]]:
    return [(i, text[i:i + _SEGMENT]) for i in range(0, len(text), _SEGMENT)]


def _hanlp_batch(chunks: list[str]) -> list[list[Span]]:
    """对一个文本块的列表批量跑 HanLP（一次 forward，比逐块快数倍）。"""
    if _han_tok is None or _han_ner is None or not chunks:
        return [[] for _ in chunks]
    try:
        token_lists = [[str(t) for t in toks] for toks in _han_tok(chunks)]
        all_items = _han_ner(token_lists)
    except Exception:  # noqa: BLE001
        return [[] for _ in chunks]

    out: list[list[Span]] = []
    for idx, tokens in enumerate(token_lists):
        offsets = _token_offsets(chunks[idx], tokens)
        items = all_items[idx] if idx < len(all_items) else []
        spans: list[Span] = []
        for item in items or []:
            try:
                _word, label, ts, te = item
            except (TypeError, ValueError):
                continue
            typ = _HAN_LABEL_MAP.get(str(label).upper())
            if not typ or ts >= len(offsets) or te - 1 >= len(offsets):
                continue
            s0 = offsets[ts][0]
            s1 = offsets[te - 1][1]
            if s1 > s0:
                spans.append(Span(s0, s1, typ, _NER_SCORE, "ner"))
        out.append(spans)
    return out


def _hanlp_spans(text: str) -> list[Span]:
    if _han_tok is None or _han_ner is None:
        return []
    parts = _chunks(text)
    results = _hanlp_batch([c for _, c in parts])
    out: list[Span] = []
    for (start, _chunk), spans in zip(parts, results):
        for s in spans:
            out.append(Span(start + s.start, start + s.end, s.type, s.score, s.source))
    return out


def ner_spans_batch(texts: list[str]) -> list[list[Span]]:
    """批量识别多个文本，返回与输入等长的 Span 列表。

    批量接口的价值在于：HanLP 一次 forward 可以吃多段文本，
    逐条调用时每条的固定开销（≈50~150 ms）会累加成分钟级。
    """
    if not texts:
        return []
    load_models()
    out: list[list[Span]] = [[] for _ in texts]

    han_idx: list[int] = []
    chunks: list[str] = []
    owners: list[tuple[int, int]] = []
    for i, t in enumerate(texts):
        if not t or not t.strip():
            continue
        if _chinese_ratio(t) > _HAN_RATIO:
            han_idx.append(i)
            for start in range(0, len(t), _SEGMENT):
                chunks.append(t[start:start + _SEGMENT])
                owners.append((i, start))
        else:
            out[i] = _spacy_spans(t)

    if chunks:
        results = _hanlp_batch(chunks)
        for (owner, offset), spans in zip(owners, results):
            for s in spans:
                out[owner].append(Span(offset + s.start, offset + s.end, s.type, s.score, s.source))
    return out


def _spacy_spans(text: str) -> list[Span]:
    if _nlp is None:
        return []
    out: list[Span] = []
    for start in range(0, len(text), _SEGMENT):
        chunk = text[start:start + _SEGMENT]
        if not chunk.strip():
            continue
        try:
            doc = _nlp(chunk)
        except Exception:  # noqa: BLE001
            continue
        for ent in doc.ents:
            typ = _SPACY_LABEL_MAP.get(ent.label_)
            if not typ:
                continue
            out.append(Span(start + ent.start_char, start + ent.end_char, typ, _NER_SCORE, "ner"))
    return out


def ner_spans(text: str) -> list[Span]:
    if not text or not text.strip():
        return []
    load_models()
    if _chinese_ratio(text) > _HAN_RATIO:
        return _hanlp_spans(text)
    return _spacy_spans(text)
