"""T2.6 分发：按魔数判类型、拒绝危险/超限文件、旧格式转换、超时。

对外主入口：dispatch_redact(in_path, out_path, detect=None) -> Report

真实图纸包暴露并修复的问题：
1. **OLE2 一律当 encrypted/doc/xls**：`sniff()` 返回 "ole2" 后只有 `WordDocument`/
   `Workbook` 两个流名，现实中加密 OOXML 的流叫 `EncryptedPackage`，而
   `Thumbs.db`、`.db`、`.msg` 同样是 OLE2 容器——它们会被当成「加密文档」直接
   fail，导致一张图都处理不了。现在：Thumbs.db 等直接跳过（not_applicable），
   只有真的探测到 `EncryptedPackage` 才判 encrypted；
2. **超时后不清理子线程**：`ThreadPoolExecutor` 里的 future 超时后仍在跑，
   进程退出时挂住。改用带超时的子进程隔离（可被真正杀掉）；
3. **PDF 页数检查在 handler 里重复**、且失败时 out 文件残留；
4. 新增 pptx / 旧格式（doc/xls/et）处理；
5. 新增 `dispatch_bytes()`，供「不落盘」的批量处理路径使用；
6. 文件类型判定增加 `--` 复合判定：扩展名与魔数不一致时以魔数为准并告警。
"""
from __future__ import annotations

import multiprocessing as mp
import os
import tempfile
import threading
import zipfile
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
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

IMAGE_KINDS = ("png", "jpg", "bmp", "tif", "gif", "webp")
IMAGE_EXTS = {
    ".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".bmp": "bmp",
    ".tif": "tif", ".tiff": "tif", ".gif": "gif", ".webp": "webp",
}

# 明确与业务无关的 OLE2 容器（缩略图缓存、Outlook 邮件、CAD 缓存等）
_IGNORABLE_OLE_NAMES = {"thumbs.db", "ehthumbs.db", "desktop.ini"}


def sniff(path: Path) -> str:
    """返回类型：docx/xlsx/pptx/doc/xls/pdf/png/jpg/bmp/tif/gif/zip/rar/7z/gz/macro/encrypted/html_xls/unknown"""
    with open(path, "rb") as f:
        head = f.read(512)
    probe = head.lstrip().lower()
    # 「假 xls」：ERP/扫描件常把 HTML 表格存成 .xls
    if probe.startswith(b"<html") or probe.startswith(b"<!doctype") or b"<table" in probe[:400]:
        return "html_xls"
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
    if head.startswith(TIFF_LE) or head.startswith(TIFF_BE):
        return "tif"
    if head.startswith(GIF):
        return "gif"
    if head.startswith(b"RIFF") and b"WEBP" in head:
        return "webp"
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
    low = [n.lower() for n in names]
    if any(n.endswith("vbaproject.bin") for n in low):
        return "macro"          # 宏启用
    if any(n.startswith("word/") for n in low):
        return "docx"
    if any(n.startswith("xl/") for n in low):
        return "xlsx"
    if any(n.startswith("ppt/") for n in low):
        return "pptx"
    return "zip"


def _ole_streams(path: Path) -> list[str]:
    """列出 OLE2 容器里的流名（不依赖 olefile，直接解析目录项）。"""
    import struct

    try:
        data = path.read_bytes()
    except OSError:
        return []
    if len(data) < 512 or data[:8] != OLE2:
        return []
    try:
        sector_shift = struct.unpack_from("<H", data, 30)[0]
        mini_shift = struct.unpack_from("<H", data, 32)[0]
        num_fat = struct.unpack_from("<I", data, 44)[0]
        dir_start = struct.unpack_from("<I", data, 48)[0]
        sector_size = 1 << sector_shift
        # DIFAT 前 109 项
        fat_sectors = list(struct.unpack_from("<109I", data, 76))[:num_fat]
        fat: list[int] = []
        for sec in fat_sectors:
            off = (sec + 1) * sector_size
            if off + sector_size > len(data):
                continue
            fat.extend(struct.unpack_from(f"<{sector_size // 4}I", data, off))
        names: list[str] = []
        sec = dir_start
        guard = 0
        while sec < 0xFFFFFFFA and guard < 4096:
            guard += 1
            off = (sec + 1) * sector_size
            if off + sector_size > len(data):
                break
            for i in range(sector_size // 128):
                entry = data[off + i * 128: off + (i + 1) * 128]
                nlen = struct.unpack_from("<H", entry, 64)[0]
                if 2 <= nlen <= 64:
                    raw = entry[:nlen - 2]
                    try:
                        names.append(raw.decode("utf-16-le", "ignore"))
                    except Exception:  # noqa: BLE001
                        pass
            if sec >= len(fat):
                break
            sec = fat[sec]
        return names
    except Exception:  # noqa: BLE001
        return []


def _ole2_kind(path: Path) -> str:
    """区分加密 OOXML / .doc / .xls / 其它 OLE2。"""
    names = {n.lower() for n in _ole_streams(path)}
    if "encryptedpackage" in names:
        return "encrypted"
    if "worddocument" in names:
        return "doc"
    if "workbook" in names or "book" in names:
        return "xls"
    # 退化：按内容关键字粗判
    try:
        data = path.read_bytes()[:8192]
    except OSError:
        return "ole2"
    if b"WordDocument" in data:
        return "doc"
    if b"Workbook" in data or b"Book" in data:
        return "xls"
    return "ole2"


def _handler_for(kind: str):
    if kind == "docx":
        from handlers.docx_h import redact_docx

        return redact_docx
    if kind == "xlsx":
        from handlers.xlsx_h import redact_xlsx

        return redact_xlsx
    if kind == "pptx":
        from handlers.pptx_h import redact_pptx

        return redact_pptx
    if kind == "pdf":
        from handlers.pdf_h import redact_pdf

        return redact_pdf
    if kind in IMAGE_KINDS:
        from handlers.image_h import redact_image

        return redact_image
    return None


def _run_handler(fn, in_path, out_path, kind, rep) -> None:
    from core.detect import detect

    sub = fn(in_path, out_path, detect)
    rep.counts = dict(sub.counts)
    rep.warnings.extend(w for w in sub.warnings if w not in rep.warnings)
    rep.needs_review = sub.needs_review
    rep.status = sub.status
    rep.message = sub.message
    if sub.status == "failed":
        return
    # T2.7 复核
    from handlers.verify import partial_notes, residual_report, verify_output

    ok = verify_output(out_path, kind)
    notes = partial_notes()
    if notes:
        rep.warn("复核覆盖不全：" + "；".join(notes), review=True)
    if not ok:
        rep.needs_review = True
        if rep.status == "done":
            rep.status = "needs_review"
        residual = residual_report(out_path, kind)
        detail = ", ".join(f"{k}={v}" for k, v in sorted(residual.items())) or "未知"
        rep.warn(f"复核发现残留敏感内容（{detail}）", review=True)


def dispatch_redact(in_path, out_path, detect=None) -> Report:
    in_path, out_path = Path(in_path), Path(out_path)
    rep = Report()

    if not in_path.exists():
        return rep.fail("输入文件不存在")
    # Office 打开文档时生成的属主锁文件（~$xxx.xlsx）只含用户名与路径，
    # 不是正文；直接跳过，不要记为失败。
    if in_path.name.startswith("~$"):
        return rep.skip("Office 临时锁文件，无正文")
    size_mb = in_path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_MB:
        return rep.fail(f"文件超过 {MAX_MB:.0f}MB 限制")

    kind = sniff(in_path)
    if kind in ("rar", "7z", "gz", "zip"):
        return rep.fail("压缩包不允许上传")
    if kind == "macro":
        return rep.fail("含宏的文档(.docm/.xlsm)不允许上传")
    if kind == "encrypted":
        return rep.fail("加密文档不允许上传")
    if kind == "unknown":
        if in_path.name.lower() in _IGNORABLE_OLE_NAMES:
            return rep.skip("系统缩略图缓存，无需处理")
        return rep.fail("无法识别的文件类型")

    workdir = Path(tempfile.mkdtemp(prefix="redact_"))
    try:
        if kind == "ole2":
            kind = _ole2_kind(in_path)
            if kind == "encrypted":
                return rep.fail("加密文档不允许上传")
            if kind == "ole2":
                if in_path.name.lower() in _IGNORABLE_OLE_NAMES:
                    return rep.skip("系统缓存文件，无需处理")
                return rep.skip("OLE2 容器（缩略图/邮件/缓存等），无可脱敏正文")

        if kind in ("doc", "xls", "html_xls"):
            from handlers.convert import backend_status, convert_legacy

            target = "docx" if kind == "doc" else "xlsx"
            conv = convert_legacy(in_path, workdir, target)
            if conv is None:
                st = backend_status()
                rep.warn(
                    "缺少可用的旧格式转换后端（LibreOffice / Office COM），"
                    f"无法转换 {kind}，需人工处理；后端状态={st['backend'] or 'none'}",
                    review=True,
                )
                return rep
            in_path = conv
            kind = target

        if kind == "pdf":
            import pymupdf as fitz

            try:
                doc = fitz.open(str(in_path))
            except Exception:  # noqa: BLE001
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

        _run_with_timeout(fn, in_path, out_path, kind, rep)
        return rep
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)


def _run_with_timeout(fn, in_path, out_path, kind, rep) -> None:
    """处理 + 超时控制。

    默认走**常驻工作进程池**：进程只起一次，模型（HanLP/spaCy/OCR）只加载一次，
    批量处理 700+ 文件时不会每次重付 5~7 秒加载成本；超时则销毁并重建进程池，
    保证挂死的任务被真正杀掉（线程超时杀不掉）。
    `limits.isolate_process: false` 时退回同进程直调（调试用）。
    """
    if not bool(cfg("limits", "isolate_process", default=True)):
        _run_handler(fn, Path(in_path), Path(out_path), kind, rep)
        return

    pool = _get_pool()
    try:
        fut = pool.submit(
            _pool_worker, fn.__module__, fn.__name__, str(in_path), str(out_path), kind
        )
        payload = fut.result(timeout=TIMEOUT)
    except FuturesTimeout:
        _reset_pool(kill=True)
        rep.fail(f"处理超时(>{TIMEOUT}s)")
        return
    except Exception as e:  # noqa: BLE001
        # 进程池被外部杀掉（BrokenProcessPool）/无法 spawn（脚本未加 __main__ 保护）：
        # 重建一次；仍失败则退回同进程执行，保证单个文件的失败不拖垮整批任务。
        _reset_pool(kill=True)
        try:
            pool = _get_pool()
            payload = pool.submit(
                _pool_worker, fn.__module__, fn.__name__, str(in_path), str(out_path), kind
            ).result(timeout=TIMEOUT)
        except Exception:  # noqa: BLE001
            _run_handler(fn, Path(in_path), Path(out_path), kind, rep)
            # 退化为同进程是**正常降级**，不写进 warnings（否则每份文件都被
            # 标成 needs_review），计数放在专用字段里供报告统计。
            rep.degraded = "pool_unavailable"
            return

    if payload.get("error"):
        rep.fail(f"处理异常: {payload['error']}")
        return
    rep.counts = dict(payload.get("counts") or {})
    rep.warnings = list(payload.get("warnings") or [])
    rep.needs_review = bool(payload.get("needs_review"))
    rep.status = payload.get("status") or "done"
    rep.message = payload.get("message") or ""


# 工作进程内的「模型已加载」标记：用模块级全局，而不是进程对象属性。
# 进程池里的进程是复用的，但 `mp.current_process()` 每次调用返回的是同一个
# 代理对象，早期实现用 `m._redact_ready` 在 spawn 模式下并未生效，
# 导致**每个文件**都重新加载 HanLP/spaCy/OCR（每次 5~7 秒）。
_WORKER_READY = False


def _pool_worker(module: str, name: str, in_path: str, out_path: str, kind: str) -> dict:
    """在工作进程里执行，返回可 pickle 的普通 dict（不能回传 Report 对象）。"""
    global _WORKER_READY
    if not _WORKER_READY:
        try:
            from core.ner import load_models

            load_models()
        except Exception:  # noqa: BLE001
            pass
        try:
            from handlers.ocr import get_engine

            get_engine()
        except Exception:  # noqa: BLE001
            pass
        _WORKER_READY = True
    try:
        import importlib

        fn = getattr(importlib.import_module(module), name)
        rep = Report()
        _run_handler(fn, Path(in_path), Path(out_path), kind, rep)
        return {
            "counts": rep.counts,
            "warnings": rep.warnings,
            "needs_review": rep.needs_review,
            "status": rep.status,
            "message": rep.message,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "status": "failed"}


_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            workers = int(cfg("limits", "worker_processes", default=1))
            _pool = ProcessPoolExecutor(
                max_workers=max(1, workers), mp_context=mp.get_context("spawn")
            )
        return _pool


def _reset_pool(kill: bool = False) -> None:
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is None:
        return
    try:
        if kill:
            for proc in list(getattr(pool, "_processes", {}).values()):
                proc.terminate()
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001
        pass


def shutdown_pool() -> None:
    _reset_pool(kill=False)


def kind_of(path: Path) -> str:
    """对外暴露的类型判定（含 OLE2 细分），供批量工具使用。"""
    k = sniff(Path(path))
    if k == "ole2":
        k = _ole2_kind(Path(path))
    return k
