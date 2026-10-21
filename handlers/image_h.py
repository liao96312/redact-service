"""T2.4 图片处理器：OCR 识别 -> 命中片段涂黑 -> 清除 EXIF。

签名：redact_image(in_path, out_path, detect) -> Report

真实截图（微信/淘宝聊天记录）暴露的问题及修正：
1. 旧实现按「字符数比例」估算命中横坐标，中英混排时必然涂偏，实测手机号只涂掉
   前几位。改为优先使用 OCR 的逐字框（`words`），逐字累积求包围盒；
2. 命中框高度只取「该行文字框」，字距较大的行会漏掉标点，统一上下各扩 2px；
3. 输入可能是 RGBA/P 模式（截图常见），统一 convert("RGB") 后再画；
4. 图片格式以实际文件头为准（很多 .jpg 实际是 PNG），避免 PIL 存错导致二次损坏；
5. 大面积命中（长文本段）直接按整行涂黑，避免逐字框之间的空隙泄露。
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw

from core.config import cfg
from core.span import Span
from handlers.base import Report
from handlers.ocr import ocr_image

_CONF_TH = float(cfg("limits", "ocr_line_conf_threshold", default=0.80))
# 低置信行占比达到该阈值才升级为 needs_review（单条低分只是噪声）
_LOW_CONF_RATIO = float(cfg("limits", "ocr_low_conf_ratio", default=0.60))
_MIN_FRAG = int(cfg("limits", "min_fragment_chars", default=2))
_USE_NER = bool(cfg("ocr", "detect_use_ner", default=False))
_PAD = int(cfg("limits", "image_box_pad_px", default=2))


def _box_rect(box) -> tuple[float, float, float, float]:
    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def _is_meaningful(text: str) -> bool:
    """低置信度告警只针对「有实际含义的文字行」。

    实测：仓库/质检现场照里满是标签、编号、箭头符号，OCR 会输出大量
    `①~各`、`-00/ 门4 ①①`、`kgNo` 这类低置信碎片，逐条告警会把整批现场照
    都打成 needs_review，复核列表彻底失效。这里要求：
    - 至少 3 个字符；
    - 至少要有一个汉字或两个连续字母（排除纯编号/符号）；
    - 不能是纯数字/编号形态。
    """
    t = (text or "").strip()
    if len(t) < 3:
        return False
    han = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fa5")
    if han >= 2:
        return True
    if re.search(r"[A-Za-z]{3,}", t) and not re.fullmatch(r"[A-Za-z]{1,4}[-/]?\d*", t):
        return True
    return False


def _fragment_rect(box, text: str, span: Span, words=None) -> tuple[float, float, float, float]:
    """把命中片段映射到该行的子框：优先逐字框，退化到按字符比例。"""
    x0, y0, x1, y1 = _box_rect(box)
    if words and len(words) >= len(text):
        chosen = words[span.start:span.end]
        if chosen:
            return (
                min(w[0] for w in chosen),
                min(w[1] for w in chosen),
                max(w[2] for w in chosen),
                max(w[3] for w in chosen),
            )
    n = max(len(text), 1)
    rx0 = x0 + (x1 - x0) * (span.start / n)
    rx1 = x0 + (x1 - x0) * (span.end / n)
    return rx0, y0, rx1, y1


def _sniff_format(in_path: Path, img: Image.Image) -> str:
    """以文件头判断真实格式，返回 PIL 保存用的格式名。"""
    head = in_path.read_bytes()[:12]
    if head.startswith(b"\x89PNG"):
        return "PNG"
    if head.startswith(b"\xff\xd8"):
        return "JPEG"
    if head.startswith(b"BM"):
        return "BMP"
    if head.startswith((b"II\x2a\x00", b"MM\x00\x2a")):
        return "TIFF"
    if head.startswith(b"GIF8"):
        return "GIF"
    if head.startswith(b"RIFF") and b"WEBP" in head:
        return "WEBP"
    return (img.format or "PNG").upper()


def redact_image(in_path, out_path, detect) -> Report:
    in_path, out_path = Path(in_path), Path(out_path)
    try:
        img = Image.open(in_path)
        img.load()
    except Exception as e:  # noqa: BLE001
        return Report().fail(f"图片无法打开: {type(e).__name__}")

    fmt = _sniff_format(in_path, img)
    base, rep = redact_pil(img, detect)

    # 清除 EXIF：以无 exif 的方式另存
    save_kwargs: dict = {}
    if fmt == "JPEG":
        save_kwargs = {"quality": 92, "subsampling": 0}
        if base.mode not in ("RGB", "L"):
            base = base.convert("RGB")
    elif fmt == "PNG":
        if base.mode not in ("RGB", "L", "RGBA"):
            base = base.convert("RGB")
    elif fmt in ("BMP", "TIFF", "GIF", "WEBP"):
        pass
    else:
        fmt = "PNG"
    try:
        base.save(out_path, format=fmt, **save_kwargs)
    except Exception as e:  # noqa: BLE001
        rep.fail(f"图片保存失败: {type(e).__name__}")
    return rep


def redact_pil(img: Image.Image, detect, rep: Report | None = None):
    """对 PIL 图片做 OCR 打码，返回 (新图, Report)。"""
    if rep is None:
        rep = Report()
    try:
        lines = ocr_image(img)
    except Exception as e:  # noqa: BLE001
        rep.fail(f"OCR 失败: {type(e).__name__}")
        return img.convert("RGB"), rep

    base = img.convert("RGB")
    draw = ImageDraw.Draw(base)

    low_conf = 0
    for ln in lines:
        text, box, score = ln["text"], ln["box"], ln["score"]
        words = ln.get("words")
        # 低置信度只累计，最后统一决定是否升级为 needs_review：
        # 现场照/截图里的零星低分碎片是常态，逐条告警会让复核列表失效。
        if score < _CONF_TH and _is_meaningful(text):
            low_conf += 1
        spans = detect(text, use_ner=_USE_NER)
        for sp in spans:
            rep.add(sp.type)
            if sp.length < _MIN_FRAG:
                rect = _box_rect(box)
            else:
                rect = _fragment_rect(box, text, sp, words)
            draw.rectangle(
                [rect[0] - _PAD, rect[1] - _PAD, rect[2] + _PAD, rect[3] + _PAD],
                fill=(0, 0, 0),
            )

    total = max(1, len([ln for ln in lines if _is_meaningful(ln["text"])]))
    ratio = low_conf / total
    if ratio >= _LOW_CONF_RATIO:
        rep.warn(
            f"OCR 低置信行占比 {low_conf}/{total}（≥{_LOW_CONF_RATIO:.0%}），"
            "图片可能模糊/倾斜，建议人工抽查",
            review=True,
        )
    return base, rep
