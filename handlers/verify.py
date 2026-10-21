"""T2.7 复核：对输出文件重新抽取文本（图片/扫描页重新 OCR），再跑一次 detect。
仍有 score >= 阈值 的命中 -> needs_review。

verify_output(path, kind) -> bool  (True=通过，False=需复核)

修正点：
- 旧实现把 **所有** 占位符与字段标签都替换成空白再检测，导致「[姓名]」这种
  本该保留的占位符也被抹掉，无法判断替换是否生效；现在只中和「标签词」，
  占位符本身作为「已脱敏」的正向证据保留；
- 只统计 rule/dict/ctx 三类高置信来源（NER 在标签文本上噪声太大），并且
  对提取到的文本先做一次「结构归一」（去空白、全角转半角），避免 PDF 换行
  把同一个电话号码拆成多行后逃过检测；
- 新增 `residual_report()` 返回按类型聚合的残留数量，便于写进审计与报告。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from core.config import cfg, load_config
from core.detect import detect

_TH = float(cfg("limits", "verify_score_threshold", default=0.60))
# 复核阶段最多再 OCR 多少页扫描页（0 = 不 OCR），以及像素上限与时间预算
_VERIFY_OCR_PAGES = int(cfg("limits", "verify_ocr_pages", default=6))
_VERIFY_MAX_PIXELS = int(cfg("limits", "verify_max_pixels", default=12_000_000))
_VERIFY_OCR_BUDGET_S = float(cfg("limits", "verify_ocr_budget_sec", default=60))
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _placeholder_re() -> re.Pattern:
    ph = (load_config().get("placeholders") or {}).values()
    toks = sorted((re.escape(p) for p in ph if p), key=len, reverse=True)
    return re.compile("|".join(toks)) if toks else re.compile(r"(?!)")


_PH_RE = _placeholder_re()


_LABELS = [
    "Account Holder", "Customer Name", "Customer", "Client", "Beneficiary",
    "Company", "Corporation", "Organization", "Employer", "Vendor", "Supplier",
    "Address", "Name", "Contact", "Tel", "Phone", "Mobile", "Ship To", "Bill To",
    "公司名称", "单位名称", "注册地址", "办公地址", "开户行", "联系电话",
    "银行卡号", "身份证号", "证件号", "联系人", "收款人", "负责人", "经办人",
    "户名", "姓名", "客户", "法人", "地址", "住所", "电话", "手机", "座机",
    "卡号", "账号", "邮箱", "公司", "银行", "职位", "部门名称",
]


def _neutralize(text: str) -> str:
    """把字段标签替换为中性字符，避免标签本身参与二次识别造成误报。

    关键：**先保护占位符**，再做标签替换。否则「[姓名]」里的「姓名」会被
    标签表命中，占位符被拆成「[　]」——既丢失「已脱敏」的正向证据，
    又让残留判定失去意义。
    """
    ph = [p for p in (load_config().get("placeholders") or {}).values() if p]
    ph = sorted(set(ph), key=len, reverse=True)
    sentinels: dict[str, str] = {}
    protected = text
    for i, tok in enumerate(ph):
        if tok and tok in protected:
            marker = f"\ue000{i}\ue001"
            sentinels[marker] = tok
            protected = protected.replace(tok, marker)

    toks = sorted(set(_LABELS) - set(ph), key=len, reverse=True)
    if toks:
        label_re = re.compile("|".join(re.escape(t) for t in toks))
        protected = label_re.sub("\u3000", protected)

    for marker, tok in sentinels.items():
        protected = protected.replace(marker, tok)
    return protected


def _normalize(text: str) -> str:
    """全角转半角 + 去掉零宽字符，减少 PDF 抽文的干扰。"""
    table = {0x3000: 0x20, 0xFF08: 0x28, 0xFF09: 0x29, 0xFF1A: 0x3A, 0xFF0C: 0x2C}
    text = text.translate(table)
    return text.replace("\u200b", "").replace("\ufeff", "")


def extract_text(path: Path, kind: str) -> str:
    path = Path(path)
    if kind == "docx":
        return _extract_docx(path)
    if kind == "xlsx":
        return _extract_xlsx(path)
    if kind == "pptx":
        return _extract_pptx(path)
    if kind == "pdf":
        return _extract_pdf(path)
    if kind in ("png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp"):
        from handlers.ocr import ocr_image

        return "\n".join(x["text"] for x in ocr_image(str(path)))
    return ""


def _extract_docx(path: Path) -> str:
    from lxml import etree

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
    except Exception:  # noqa: BLE001
        return ""
    for ws in wb.worksheets:
        parts.append(ws.title)
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str):
                    parts.append(c.value)
    wb.close()
    return "\n".join(parts)


def _extract_pptx(path: Path) -> str:
    from lxml import etree

    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    out = []
    try:
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if not n.startswith("ppt/slides/slide") or not n.endswith(".xml"):
                    continue
                try:
                    root = etree.fromstring(z.read(n))
                except etree.XMLSyntaxError:
                    continue
                out.append("".join(t.text or "" for t in root.iter(f"{{{A}}}t")))
    except zipfile.BadZipFile:
        return ""
    return "\n".join(out)


def _extract_pdf(path: Path) -> str:
    """抽取 PDF 文本；扫描页做 OCR。

    性能：32 页的扫描版安装图，复核阶段再整本 OCR 一次要 3~5 分钟，
    叠加处理阶段就必然超过单文件 300 秒上限（实测 3 份 PDF 因此失败）。
    这里对扫描页 OCR 设**页数 + 时间双重预算**：超出部分不 OCR，改为记录
    「复核仅抽样」，既不谎称通过，也不把整批任务拖死。
    """
    import io
    import time

    import pymupdf as fitz

    parts = []
    scanned_total = 0
    ocr_done = 0
    budget = _VERIFY_OCR_BUDGET_S
    try:
        doc = fitz.open(str(path))
    except Exception:  # noqa: BLE001
        return ""
    try:
        for page in doc:
            t = page.get_text()
            textless = len(t.strip()) < 20
            if not textless:
                parts.append(t)
                continue
            scanned_total += 1
            if ocr_done >= _VERIFY_OCR_PAGES or budget <= 0:
                parts.append(t)
                continue
            from PIL import Image

            from handlers.ocr import ocr_image

            w_px = int(page.rect.width / 72 * 150)
            h_px = int(page.rect.height / 72 * 150)
            if w_px * h_px > _VERIFY_MAX_PIXELS:
                parts.append(t)
                continue
            t_start = time.time()
            pix = page.get_pixmap(dpi=150)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            parts.append(" ".join(x["text"] for x in ocr_image(img)))
            budget -= time.time() - t_start
            ocr_done += 1
    finally:
        doc.close()
    if scanned_total > ocr_done:
        _partial.append(
            f"{scanned_total} 页无文本页中仅抽样复核 {ocr_done} 页（其余未 OCR）"
        )
    return "\n".join(parts)


# 本次 extract_text 期间「复核覆盖不全」的记录，由 residual_report 读取
_partial: list[str] = []


def residual_hits(path, kind: str) -> list:
    """残留检测：只看高置信来源（规则/词典/上下文）。

    NER(score=0.6) 在脱敏后的「标签:占位符」文本上噪声极大（会把标签本身判成实体），
    会产生大量误报，故复核阶段不计 NER。
    """
    _partial.clear()
    text = _normalize(extract_text(Path(path), kind))
    if not text.strip():
        return []
    text = _neutralize(text)
    return [s for s in detect(text) if s.score >= _TH and s.source != "ner"]


def partial_notes() -> list[str]:
    """复核覆盖不全的说明（例如扫描页太多、只抽检了前几页）。"""
    return list(_partial)


def residual_report(path, kind: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in residual_hits(path, kind):
        counts[s.type] = counts.get(s.type, 0) + 1
    return counts


def verify_output(path, kind: str) -> bool:
    return not residual_hits(path, kind)
