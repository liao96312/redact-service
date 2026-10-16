"""T2.6 分发：按魔数判类型、拒绝危险/超限文件、旧格式转换、超时。

对外主入口：dispatch_redact(in_path, out_path, detect=None) -> Report
"""
from __future__ import annotations

import subprocess
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from pathlib import Path

from core.config import cfg
from handlers.base import Report

MAX_MB = float(cfg("limits", "max_file_mb", default=50))
MAX_PAGES = int(cfg("limits", "max_pages", default=200))
TIMEOUT = int(cfg("limits", "file_timeout_sec", default=300))

# 魔数
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP = b"\x50\x4b\x03\x04"
PDF = b"%PDF"
PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
BMP = b"BM"
GIF = b"GIF8"
TIFF_LE = b"II\x2a\x00"
TIFF_BE = b"MM\x00\x2a"
RAR = b"Rar!\x1a\x07"
SEVENZ = b"7z\xbc\xaf\x27\x1c"
GZIP = b"\x1f\x8b"

IMAGE_EXTS = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".bmp": "bmp", ".tif": "tif", ".tiff": "tif"}

try:
    from lxml import etree  # noqa

    _HAS_LXML = True
except Exception:  # pragma: no cover
    _HAS_LXML = False


def sniff(path: Path) -> str:
    """返回类型：docx/xlsx/pptx/doc/xls/pdf/png/jpg/bmp/tif/gif/zip/rar/7z/gz/unknown"""
    with open(path, "rb") as f:
        head = f.read(16)
    if head.startswith(OLE2):
        return "ole2"
    if head.startswith(PDF):
        return "pdf"
    if head.startswith(PNG):
        return "png"
    if head.startswith(JPEG):
        return "jpg"
    if head.startswith(BMP):
        return "bmp"
    if head.startswith((TIFF_LE, TIFF_BE)):
        return "tif"
    if head.startswith(GIF):
        return "gif"
    if head.startswith(ZIP):
        return _zip_kind(path)
    if head.startswith(RAR):
        return "rar"
    if head.startswith(SEVENZ):
        return "7z"
    if head.startswith(GZIP):
        return "gz"
    return "unknown"


def _zip_kind(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except zipfile.BadZipFile:
        return "zip"
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        return "macro"          # 宏启用
    if any(n.startswith("word/") for n in names):
        return "docx"
    if any(n.startswith("xl/") for n in names):
        return "xlsx"
    if any(n.startswith("ppt/") for n in names):
        return "pptx"
    return "zip"


def _ole2_kind(path: Path) -> str:
    """区分加密 OOXML / .doc / .xls。"""
    try:
        import olefile  # 可选

        if olefile.isOleFile(str(path)):
            ole = olefile.OleFileIO(str(path))
            if ole.exists("EncryptedPackage"):
                return "encrypted"
            for stream in ("WordDocument",):
                if ole.exists(stream):
                    return "doc"
            for stream in ("Workbook", "Book"):
                if ole.exists(stream):
                    return "xls"
    except Exception:
        pass
    # 退化：按内容关键字粗判
    data = path.read_bytes()[:8192]
    if b"WordDocument" in data:
        return "doc"
    if b"Workbook" in data or b"Book" in data:
        return "xls"
    return "ole2"


def _soffice_available() -> bool:
    from shutil import which

    return which("soffice") is not None or which("soffice.exe") is not None


def convert_legacy(in_path: Path, out_dir: Path, target: str) -> Path | None:
    """用 LibreOffice headless 将 .doc/.xls 转为 xlsx/docx。返回新路径或 None。"""
    if not _soffice_available():
        return None
    cmd = ["soffice", "--headless", "--convert-to", target, "--outdir", str(out_dir), str(in_path)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=TIMEOUT)
    except Exception:
        return None
    out = out_dir / (in_path.stem + "." + target)
    return out if out.exists() else None


def dispatch_redact(in_path, out_path, detect=None) -> Report:
    in_path, out_path = Path(in_path), Path(out_path)
    rep = Report()

    size_mb = in_path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_MB:
        return rep.fail(f"文件超过 {MAX_MB}MB 限制")

    kind = sniff(in_path)
    if kind in ("rar", "7z", "gz", "zip"):
        return rep.fail("压缩包不允许上传")
    if kind == "macro":
        return rep.fail("含宏的文档(.docm/.xlsm)不允许上传")
    if kind == "pptx":
        return rep.fail("暂不支持 pptx")
    if kind == "encrypted":
        return rep.fail("加密文档不允许上传")

    workdir = Path(tempfile.mkdtemp(prefix="redact_"))
    try:
        if kind == "ole2":
            kind = _ole2_kind(in_path)
            if kind == "encrypted":
                return rep.fail("加密文档不允许上传")
        if kind in ("doc", "xls"):
            target = "xlsx" if kind == "xls" else "docx"
            conv = convert_legacy(in_path, workdir, target)
            if conv is None:
                rep.status = "needs_review"
                rep.warn("缺少 LibreOffice，无法转换旧格式，需人工处理", review=True)
                return rep
            in_path = conv
            kind = "xlsx" if kind == "xls" else "docx"

        if kind == "pdf":
            import pymupdf as fitz

            try:
                doc = fitz.open(str(in_path))
            except Exception:
                return rep.fail("PDF 无法打开（文件损坏或非法）")
            try:
                if doc.needs_pass:
                    return rep.fail("加密 PDF 不允许上传")
                if doc.page_count > MAX_PAGES:
                    return rep.fail(f"页数 {doc.page_count} 超过 {MAX_PAGES} 限制")
            finally:
                doc.close()

        fn = _handler_for(kind)
        if fn is None:
            return rep.fail(f"不支持的文件类型: {kind}")

        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_run_handler, fn, in_path, out_path, kind, rep)
            try:
                fut.result(timeout=TIMEOUT)
            except FTimeout:
                return rep.fail(f"处理超时(>{TIMEOUT}s)")
        return rep
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)


def _handler_for(kind: str):
    if kind == "docx":
        from handlers.docx_h import redact_docx

        return redact_docx
    if kind == "xlsx":
        from handlers.xlsx_h import redact_xlsx

        return redact_xlsx
    if kind == "pdf":
        from handlers.pdf_h import redact_pdf

        return redact_pdf
    if kind in ("png", "jpg", "jpeg", "bmp", "tif", "tiff"):
        from handlers.image_h import redact_image

        return redact_image
    return None


def _run_handler(fn, in_path, out_path, kind, rep) -> None:
    from core.detect import detect

    sub = fn(in_path, out_path, detect)
    rep.counts = sub.counts
    rep.warnings.extend(sub.warnings)
    rep.needs_review = sub.needs_review
    rep.status = sub.status
    rep.message = sub.message
    if sub.status == "failed":
        return
    # T2.7 复核
    from handlers.verify import verify_output

    if not verify_output(out_path, kind):
        rep.needs_review = True
        if rep.status == "done":
            rep.status = "needs_review"
        rep.warn("复核发现残留敏感内容", review=True)
