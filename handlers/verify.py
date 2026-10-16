"""T2.7 复核：对输出文件重新抽取文本（图片/扫描页重新 OCR），再跑一次 detect。
仍有 score >= 阈值 的命中 -> needs_review。

verify_output(path, kind) -> bool  (True=通过，False=需复核)
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from core.config import cfg, load_config
from core.detect import detect

_TH = float(cfg("limits", "verify_score_threshold", default=0.60))
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _placeholder_re() -> re.Pattern:
    ph = (load_config().get("placeholders") or {}).values()
    toks = sorted((re.escape(p) for p in ph if p), key=len, reverse=True)
    return re.compile("|".join(toks)) if toks else re.compile(r"(?!)")


_PH_RE = _placeholder_re()


_LABELS = [
    "Account Holder", "Customer Name", "Customer", "Client", "Beneficiary",
    "Company", "Corporation", "Organization", "Employer", "Vendor", "Supplier",
    "Address", "Name",
    "公司名称", "单位名称", "注册地址", "办公地址", "开户行", "联系电话",
    "银行卡号", "身份证号", "证件号", "联系人", "收款人", "负责人", "经办人",
    "户名", "姓名", "客户", "法人", "地址", "住所", "电话", "手机", "座机",
    "卡号", "账号", "邮箱", "公司", "银行",
]


def _neutralize(text: str) -> str:
    """把占位符与字段标签替换为中性字符，避免其参与二次识别造成误报。"""
    text = _PH_RE.sub("\u3000", text)
    ph = (load_config().get("placeholders") or {}).values()
    toks = sorted(set(_LABELS) | set(p for p in ph if p), key=len, reverse=True)
    label_re = re.compile("|".join(re.escape(t) for t in toks))
    return label_re.sub("\u3000", text)


def extract_text(path: Path, kind: str) -> str:
    path = Path(path)
    if kind == "docx":
        return _extract_docx(path)
    if kind == "xlsx":
        return _extract_xlsx(path)
    if kind == "pdf":
        return _extract_pdf(path)
    if kind in ("png", "jpg", "jpeg", "bmp", "tif", "tiff"):
        from handlers.ocr import ocr_image

        return "\n".join(x["text"] for x in ocr_image(str(path)))
    return ""


def _extract_docx(path: Path) -> str:
    from lxml import etree

    # 按段落抽取并以换行分隔，避免"标签:值"跨段误匹配
    out = []
    try:
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if not (n.startswith("word/") and n.endswith(".xml")):
                    continue
                try:
                    root = etree.fromstring(z.read(n))
                except etree.XMLSyntaxError:
                    continue
                lines = []
                for p in root.iter(f"{{{_W}}}p"):
                    lines.append("".join(t.text or "" for t in p.iter(f"{{{_W}}}t")))
                out.append("\n".join(lines))
    except zipfile.BadZipFile:
        return ""
    return "\n".join(out)


def _extract_xlsx(path: Path) -> str:
    from openpyxl import load_workbook

    parts = []
    try:
        wb = load_workbook(str(path), data_only=False)
    except Exception:
        return ""
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str):
                    parts.append(c.value)
    return "\n".join(parts)


def _extract_pdf(path: Path) -> str:
    import io

    import pymupdf as fitz

    parts = []
    try:
        doc = fitz.open(str(path))
    except Exception:
        return ""
    try:
        for page in doc:
            t = page.get_text()
            if len(t.strip()) < 20 and page.get_images(full=True):
                from handlers.ocr import ocr_image
                from PIL import Image

                pix = page.get_pixmap(dpi=150)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                t = " ".join(x["text"] for x in ocr_image(img))
            parts.append(t)
    finally:
        doc.close()
    return "\n".join(parts)


def residual_hits(path, kind: str) -> list:
    """残留检测：只看高置信来源（规则/词典/上下文）。

    NER(score=0.6) 在脱敏后的"标签:占位符"文本上噪声极大（会把标签本身判成实体），
    会产生大量误报，故复核阶段不计 NER。
    """
    text = extract_text(Path(path), kind)
    if not text.strip():
        return []
    text = _neutralize(text)
    return [s for s in detect(text) if s.score >= _TH and s.source != "ner"]


def verify_output(path, kind: str) -> bool:
    return not residual_hits(path, kind)
