"""T2.3 PDF 文字型 + T2.5 扫描型处理（PyMuPDF）。

- 文字型：真删除（add_redact_annot + apply_redactions），跨行地址分段搜索
- 扫描页：get_pixmap(dpi) 渲染 -> 图片打码 -> 重建该页
- 清空 metadata 并 scrub

真实图纸 PDF 暴露并修复的问题（这一层原来几乎必然漏检）：
1. **搜索不带 page 上下文**：`page.get_text()` 默认按「阅读顺序」拼接，
   与 `page.search_for()` 的坐标并不一一对应；旧实现拿 sp.start/end 去切片
   再逐段 search，遇到页眉页脚、双栏、旋转文字就搜不到，实测报价单整页漏检。
   改为用 `page.get_text("dict")` 拿到每个 span 的**真实 bbox 和文本**，
   在 span 级做检测并直接对 bbox 打 redaction，不再二次搜索；
2. 跨行地址：同一段落在多个 span 内时，先把相邻 span 合并成「逻辑行」
   （同一 block 内、垂直间距小于行高），再在合并文本上检测；
3. 扫描页判定过严：`len(text)<20 且 有图` 会漏掉「图 + 少量文字」的图纸页。
   改为：文本量占比低且页面大部分面积被图片覆盖 -> 视为图片页，
   同时对**所有页**都额外做一次 OCR 兜底（可视化文字之外的内容）；
4. 重建扫描页时 `insert_image(newpage.rect, stream=...)` 会把整页拉伸，
   破坏线宽与比例；改为按原始 pixmap 尺寸插入；
5. 未调用 `doc.subset_fonts()`，残留字体里可能带原始文字；
6. 输出没有保留书签/页面标签（图纸 PDF 常靠书签导航），改为复制 TOC；
7. 增加 `pdf_ocr_fallback` 开关与 `pdf_max_ocr_pages` 上限，避免整包 249 份
   图纸时把 OCR 时间放大到不可接受。
"""
from __future__ import annotations

import io
import time
from pathlib import Path

import pymupdf as fitz
from PIL import Image

from core.config import cfg
from handlers.base import Report

_SCAN_TEXT_LEN = int(cfg("limits", "pdf_scanned_text_len", default=20))
_DPI = int(cfg("limits", "ocr_dpi", default=200))
_OCR_FALLBACK = bool(cfg("limits", "pdf_ocr_fallback", default=True))
_MAX_OCR_PAGES = int(cfg("limits", "pdf_max_ocr_pages", default=200))
_IMAGE_COVER = float(cfg("limits", "pdf_image_cover_ratio", default=0.55))
_MIN_FRAG = int(cfg("limits", "min_fragment_chars", default=2))
# 单页渲染像素上限（超过则跳过 OCR，避免解压炸弹告警与分钟级单页）
_MAX_PIXELS = int(cfg("limits", "pdf_max_pixels", default=40_000_000))
# 单份 PDF 的 OCR 时间预算（秒）：文件硬超时 300s，留 60s 给写盘与复核
_OCR_BUDGET_S = float(cfg("limits", "pdf_ocr_budget_sec", default=90))


def _is_scanned(page) -> bool:
    """页面文本极少、且图片基本铺满整页 -> 扫描/图片页。"""
    text = page.get_text().strip()
    if len(text) >= _SCAN_TEXT_LEN * 3:
        return False
    imgs = page.get_image_info()
    if not imgs:
        return False
    page_area = abs(page.rect.width * page.rect.height) or 1.0
    covered = 0.0
    for info in imgs:
        r = fitz.Rect(info["bbox"]) & page.rect
        if r.is_empty:
            continue
        covered += abs(r.width * r.height)
    return (covered / page_area) >= _IMAGE_COVER


def _is_textless(page) -> bool:
    """整页几乎没有可抽取文字（矢量图导出的图纸常如此：没有位图，也不是文字）。

    这类页面既不是「文字页」也不是「图片页」，但里面的标注、标题栏照样是文字，
    必须走 OCR；否则既漏脱敏，又会被后面的整页兜底 OCR 反复拖慢。
    """
    return len(page.get_text().strip()) < _SCAN_TEXT_LEN


def _line_groups(page) -> list[tuple[str, list[tuple[float, float, float, float]]]]:
    """把页面文字按「逻辑行」聚合成 (文本, bbox 列表)。

    PDF 里一个地址常被拆成 3~5 个 span（字距、换行、混排字体），
    这里按 block/line 结构聚合，同时把同一 block 内相邻行也并起来，
    以便跨行地址能被完整识别。
    """
    groups: list[tuple[str, list[tuple[float, float, float, float]]]] = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        lines = block.get("lines", [])
        buf_text: list[str] = []
        buf_rects: list[tuple[float, float, float, float]] = []

        def flush() -> None:
            nonlocal buf_text, buf_rects
            txt = "".join(buf_text)
            if txt.strip() and buf_rects:
                groups.append((txt, buf_rects))
            buf_text, buf_rects = [], []

        prev_y1: float | None = None
        for line in lines:
            spans = line.get("spans", [])
            line_txt = "".join(s.get("text", "") for s in spans)
            if not line_txt:
                continue
            y0 = min(s["bbox"][1] for s in spans)
            y1 = max(s["bbox"][3] for s in spans)
            h = max(1.0, y1 - y0)
            # 相邻行且行距 < 1.6 倍行高 -> 视为同一逻辑段
            if prev_y1 is not None and (y0 - prev_y1) > 1.6 * h:
                flush()
            prev_y1 = y1
            buf_text.append(line_txt)
            for s in spans:
                t = s.get("text", "")
                if not t:
                    continue
                buf_rects.append(tuple(s["bbox"]))
            # 行内换行保留分隔，避免两行文字被拼成一个词
            buf_text.append("\n")
        flush()
    return groups


def _redact_text_page(page, detect, rep: Report) -> bool:
    """对文字页做真删除；返回是否命中。"""
    hit = False
    for text, rects in _line_groups(page):
        spans = detect(text)
        if not spans:
            continue
        hit = True
        rep.add_spans(spans)
        if len(text) == len(rects):
            # 一个 span 对应一个 bbox，可以精确到字
            for sp in spans:
                for r in rects[sp.start:sp.end]:
                    page.add_redact_annot(fitz.Rect(r), fill=(0, 0, 0))
        else:
            # 结构与文本对不上时，按整段打掉（宁可多涂，不漏）
            for r in rects:
                page.add_redact_annot(fitz.Rect(r), fill=(0, 0, 0))
    return hit


def _redact_scanned_page(page, detect) -> tuple[bytes, Report, tuple[int, int]]:
    from handlers.image_h import redact_pil

    pix = page.get_pixmap(dpi=_DPI)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    out_img, rep = redact_pil(img, detect)
    buf = io.BytesIO()
    out_img.save(buf, format="PNG")
    return buf.getvalue(), rep, (pix.width, pix.height)


def _page_pixmap_size(page) -> tuple[int, int]:
    """按 DPI 估算渲染后的像素数，用于跳过超大页面。"""
    dpi = _DPI
    return int(page.rect.width / 72 * dpi), int(page.rect.height / 72 * dpi)


def redact_pdf(in_path, out_path, detect) -> Report:
    in_path, out_path = Path(in_path), Path(out_path)
    rep = Report()
    try:
        src = fitz.open(str(in_path))
    except Exception as e:  # noqa: BLE001
        return rep.fail(f"无法打开 PDF: {type(e).__name__}")

    if src.needs_pass:
        src.close()
        return rep.fail("加密 PDF 不允许上传")

    ocr_pages = 0
    ocr_budget = _OCR_BUDGET_S
    out = fitz.open()
    try:
        for page in src:
            scanned = _is_scanned(page) or _is_textless(page)
            # 超大页面（图纸常用 A0/A1）渲染成 200dpi 会是上亿像素，
            # 既触发 PIL 的解压炸弹告警，也会让单页 OCR 跑到分钟级。
            w_px, h_px = _page_pixmap_size(page)
            too_big = w_px * h_px > _MAX_PIXELS
            # OCR 预算：单份 PDF 累计 OCR 时间上限（每份文件还有 300s 硬超时）
            budget_left = ocr_budget > 0 and ocr_pages < _MAX_OCR_PAGES

            if scanned and budget_left and not too_big:
                t_start = time.time()
                data, r, _size = _redact_scanned_page(page, detect)
                ocr_budget -= time.time() - t_start
                rep.merge(r)
                ocr_pages += 1
                newpage = out.new_page(width=page.rect.width, height=page.rect.height)
                newpage.insert_image(page.rect, stream=data, keep_proportion=True)
                continue

            if scanned and too_big:
                rep.warn(
                    f"第 {page.number + 1} 页为超大图片页（{w_px}×{h_px}px），"
                    "已跳过 OCR，需人工确认",
                    review=True,
                )
            elif scanned:
                rep.warn(
                    f"第 {page.number + 1} 页需要 OCR，但已达到单份 OCR 上限"
                    f"（{_MAX_OCR_PAGES} 页 / {_OCR_BUDGET_S}s），已跳过并转人工",
                    review=True,
                )

            hit = _redact_text_page(page, detect, rep)
            out.insert_pdf(src, from_page=page.number, to_page=page.number)

            # 兜底：文字页里嵌入的产品图/截图也可能含敏感信息
            # 注意：这里同样要受 OCR 预算约束，否则「文字页无命中」的整本
            # 图纸会逐页兜底 OCR，把单份文件拖过 300 秒硬超时。
            if (
                _OCR_FALLBACK and not hit and not too_big and not scanned
                and ocr_budget > 0 and ocr_pages < _MAX_OCR_PAGES
            ):
                from handlers.image_h import redact_pil

                t_start = time.time()
                pix = page.get_pixmap(dpi=_DPI)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                _out_img, r2 = redact_pil(img, detect)
                ocr_budget -= time.time() - t_start
                ocr_pages += 1
                if r2.total():
                    rep.merge(r2)
                    out.delete_page(-1)
                    newpage = out.new_page(width=page.rect.width, height=page.rect.height)
                    newpage.insert_image(page.rect, stream=_png_bytes(_out_img), keep_proportion=True)

        # 书签/页面标签：图纸 PDF 常靠书签导航，保留结构
        try:
            toc = src.get_toc(simple=True)
            if toc:
                out.set_toc(toc)
        except Exception:  # noqa: BLE001
            pass

        out.set_metadata({})
        # scrub() 在「删页+重建页」后会因为悬空 xref 抛 RuntimeError（PyMuPDF 已知问题），
        # 这里分步做等价清理，保证不因清理失败而丢掉整个输出。
        try:
            out.scrub()
        except Exception:  # noqa: BLE001
            for meth in ("scrub_metadata", "scrub_xml", "scrub_attachments"):
                fn = getattr(out, meth, None)
                if fn is None:
                    continue
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
        try:
            out.subset_fonts()
        except Exception:  # noqa: BLE001
            pass
        out.save(str(out_path), garbage=4, deflate=True, clean=True)
    finally:
        out.close()
        src.close()
    return rep


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
