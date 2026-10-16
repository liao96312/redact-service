"""T2.3 PDF 文字型 + T2.5 扫描型处理（PyMuPDF）。

- 文字型：真删除（add_redact_annot + apply_redactions），跨行地址分段搜索
- 扫描页：get_pixmap(dpi) 渲染 -> 图片打码 -> 重建该页
- 清空 metadata 并 scrub
"""
from __future__ import annotations

import io
from pathlib import Path

import pymupdf as fitz
from PIL import Image

from core.config import cfg
from handlers.base import Report

_SCAN_TEXT_LEN = int(cfg("limits", "pdf_scanned_text_len", default=20))
_DPI = int(cfg("limits", "ocr_dpi", default=200))


def _is_scanned(page) -> bool:
    text = page.get_text().strip()
    has_img = bool(page.get_images(full=True))
    return len(text) < _SCAN_TEXT_LEN and has_img


def _redact_text_page(page, detect, rep: Report) -> None:
    text = page.get_text()
    if not text:
        return
    spans = detect(text)
    for sp in spans:
        fragment = text[sp.start:sp.end]
        pieces = [p.strip() for p in fragment.split("\n")]
        hit = False
        for piece in pieces:
            if len(piece) >= 2:
                for r in page.search_for(piece):
                    if not hit:
                        rep.add(sp.type)
                        hit = True
                    page.add_redact_annot(r, fill=(0, 0, 0))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)


def _redact_scanned_page(page, detect) -> tuple[bytes, Report]:
    from handlers.image_h import redact_pil

    pix = page.get_pixmap(dpi=_DPI)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    out_img, rep = redact_pil(img, detect)
    buf = io.BytesIO()
    out_img.save(buf, format="PNG")
    return buf.getvalue(), rep


def redact_pdf(in_path, out_path, detect) -> Report:
    in_path, out_path = Path(in_path), Path(out_path)
    rep = Report()
    try:
        src = fitz.open(str(in_path))
    except Exception as e:
        return rep.fail(f"无法打开 PDF: {type(e).__name__}")

    out = fitz.open()
    try:
        for page in src:
            if _is_scanned(page):
                data, r = _redact_scanned_page(page, detect)
                for k, v in r.counts.items():
                    rep.add(k, v)
                rep.warnings.extend(r.warnings)
                if r.needs_review:
                    rep.needs_review = True
                newpage = out.new_page(width=page.rect.width, height=page.rect.height)
                newpage.insert_image(newpage.rect, stream=data)
            else:
                _redact_text_page(page, detect, rep)
                out.insert_pdf(src, from_page=page.number, to_page=page.number)

        out.set_metadata({})
        out.scrub()
        out.save(str(out_path), garbage=4, deflate=True)
    finally:
        out.close()
        src.close()
    return rep
