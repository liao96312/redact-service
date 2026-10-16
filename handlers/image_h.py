"""T2.4 图片处理器：PaddleOCR/rapidocr 识别 -> 命中片段涂黑 -> 清除 EXIF。

签名：redact_image(in_path, out_path, detect) -> Report
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from core.config import cfg
from core.span import Span
from handlers.base import Report
from handlers.ocr import ocr_image

_CONF_TH = float(cfg("limits", "ocr_line_conf_threshold", default=0.80))
_MIN_FRAG = int(cfg("limits", "min_fragment_chars", default=2))
_USE_NER = bool(cfg("ocr", "detect_use_ner", default=False))


def _box_rect(box) -> tuple[float, float, float, float]:
    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def _fragment_rect(box, text: str, span: Span) -> tuple[float, float, float, float]:
    """按字符位置比例，把命中片段映射到该行的子框。"""
    x0, y0, x1, y1 = _box_rect(box)
    n = max(len(text), 1)
    rx0 = x0 + (x1 - x0) * (span.start / n)
    rx1 = x0 + (x1 - x0) * (span.end / n)
    return rx0, y0, rx1, y1


def redact_image(in_path, out_path, detect) -> Report:
    in_path, out_path = Path(in_path), Path(out_path)
    img = Image.open(in_path)
    fmt = (img.format or "PNG").upper()
    base, rep = redact_pil(img, detect)

    # 清除 EXIF：以无 exif 的方式另存
    if fmt in ("JPEG", "JPG"):
        base.save(out_path, format="JPEG", quality=95)
    elif fmt == "PNG":
        base.save(out_path, format="PNG")
    else:
        ext = in_path.suffix.lstrip(".").upper() or "PNG"
        base.save(out_path, format=ext)
    return rep


def redact_pil(img: Image.Image, detect, rep: Report | None = None):
    """对 PIL 图片做 OCR 打码，返回 (新图, Report)。"""
    if rep is None:
        rep = Report()
    try:
        lines = ocr_image(img)
    except Exception as e:  # pragma: no cover
        rep.fail(f"OCR 失败: {type(e).__name__}")
        return img.convert("RGB"), rep

    base = img.convert("RGB")
    draw = ImageDraw.Draw(base)

    for ln in lines:
        text, box, score = ln["text"], ln["box"], ln["score"]
        if score < _CONF_TH:
            rep.warn(f"OCR 行置信度低({score:.2f})", review=True)
        spans = detect(text, use_ner=_USE_NER)
        for sp in spans:
            rep.add(sp.type)
            if sp.length < _MIN_FRAG:
                rect = _box_rect(box)
            else:
                rect = _fragment_rect(box, text, sp)
            pad = 1.0
            draw.rectangle(
                [rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad],
                fill=(0, 0, 0),
            )
    return base, rep
