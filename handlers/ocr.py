"""OCR 引擎封装（T2.4 使用）。

优先使用轻量视觉模型 rapidocr（PP-OCR 的 ONNX 版，纯 CPU、体积小）；
可通过 config.ocr.engine 切换为 paddleocr。
引擎仅在首次使用时加载一次。

修正点：
- rapidocr 1.4 支持 `return_word_box=True`，返回**逐字**框（item[3]）与逐字文本
  （item[4]）。旧实现按「字符宽度线性比例」切框，遇到中英混排/字距不等时
  会涂偏（实测截图里手机号只涂掉了前几位）。这里优先使用逐字框；
- 大图先按 `ocr.max_side` 缩放，避免 4K 截图把检测模型撑爆；
- 逐行返回 `words` 字段，供 image_h 精确落框。
"""
from __future__ import annotations

import threading

from core.config import cfg

_lock = threading.Lock()
_engine = None
_engine_name = None
_WORD_BOX = bool(cfg("ocr", "word_box", default=True))
_MAX_SIDE = int(cfg("ocr", "max_side", default=3000))


def get_engine():
    global _engine, _engine_name
    with _lock:
        if _engine is not None:
            return _engine
        name = str(cfg("ocr", "engine", default="rapidocr")).lower()
        if name == "rapidocr":
            from rapidocr_onnxruntime import RapidOCR

            _engine = RapidOCR()
        else:
            from paddleocr import PaddleOCR

            _engine = PaddleOCR(
                use_angle_cls=True,
                lang=str(cfg("ocr", "lang", default="ch")),
                show_log=False,
            )
        _engine_name = name
        return _engine


def ocr_image(img) -> list[dict]:
    """输入：路径 / ndarray / PIL Image。返回 [{box, text, score, words}]。

    box 为四点多边形 [[x,y]*4]（左上->右上->右下->左下）。
    words 为逐字框列表 [(x0,y0,x1,y1), ...]，长度与 text 一致时可精确落框。
    """
    engine = get_engine()
    payload = _to_ndarray(img)
    if _engine_name == "rapidocr":
        kwargs = {"return_word_box": True} if _WORD_BOX else {}
        result, _elapse = engine(payload, **kwargs)
        out = []
        for item in result or []:
            box, text, score = item[0], item[1], float(item[2])
            words = None
            if len(item) >= 5 and isinstance(item[4], list) and len(item[4]) == len(text):
                raw_boxes = item[3] if isinstance(item[3], list) else []
                if len(raw_boxes) == len(text):
                    words = [_rect_of_quad(b) for b in raw_boxes]
            out.append({"box": box, "text": text, "score": score, "words": words})
        return out
    # paddleocr
    result = engine.ocr(payload, cls=True)
    out = []
    for line in result or []:
        for box, (text, score) in line or []:
            out.append({"box": box, "text": text, "score": float(score), "words": None})
    return out


def _rect_of_quad(quad) -> tuple[float, float, float, float]:
    xs = [float(p[0]) for p in quad]
    ys = [float(p[1]) for p in quad]
    return min(xs), min(ys), max(xs), max(ys)


def _to_ndarray(img):
    import numpy as np
    from PIL import Image

    if isinstance(img, (str, bytes)) or hasattr(img, "__fspath__"):
        return img
    if isinstance(img, Image.Image):
        arr = np.array(img.convert("RGB"))
    else:
        arr = img
    if _MAX_SIDE and getattr(arr, "shape", (0, 0))[1] > _MAX_SIDE:
        from PIL import Image as _Image

        im = _Image.fromarray(arr)
        ratio = _MAX_SIDE / im.width
        im = im.resize((_MAX_SIDE, max(1, int(im.height * ratio))), _Image.LANCZOS)
        arr = np.array(im)
    return arr
