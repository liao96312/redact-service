"""OCR 引擎封装（T2.4 使用）。

优先使用轻量视觉模型 rapidocr（PP-OCR 的 ONNX 版，纯 CPU、体积小）；
可通过 config.ocr.engine 切换为 paddleocr。
引擎仅在首次使用时加载一次。
"""
from __future__ import annotations

import threading

from core.config import cfg

_lock = threading.Lock()
_engine = None
_engine_name = None


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
    """输入：路径 / ndarray / PIL Image。返回 [{box, text, score}]。

    box 为四点多边形 [[x,y]*4]（左上->右上->右下->左下）。
    """
    engine = get_engine()
    payload = _to_ndarray(img)
    if _engine_name == "rapidocr":
        result, _elapse = engine(payload)
        out = []
        for item in result or []:
            box, text, score = item[0], item[1], float(item[2])
            out.append({"box": box, "text": text, "score": score})
        return out
    # paddleocr
    result = engine.ocr(payload, cls=True)
    out = []
    for line in result or []:
        for box, (text, score) in line or []:
            out.append({"box": box, "text": text, "score": float(score)})
    return out


def _to_ndarray(img):
    import numpy as np
    from PIL import Image

    if isinstance(img, (str, bytes)) or hasattr(img, "__fspath__"):
        return img
    if isinstance(img, Image.Image):
        return np.array(img.convert("RGB"))
    return img
