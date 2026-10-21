"""旧格式转换（.doc / .xls / .et / .wps）后端。

项目原来的 BLOCKERS B1 记录「LibreOffice 装不上，旧格式只能人工处理」，导致
桌面图纸里 84 个 .xls + 2 个 .doc 全部进 needs_review。这里提供多后端自动降级：

1. `soffice`（LibreOffice headless）—— 首选，跨平台、无 Office 授权问题；
2. **`native_xls`（自写 BIFF8 读取器）** —— 不依赖任何外部程序，把 .xls 的
   单元格文本原样转成 xlsx（格式/图表会丢失，但**文字全部保留**，而文字正是
   敏感信息所在）。目标机 Office「产品激活失败」导致 COM 全废，这是唯一可用路径；
3. `msoffice` / `wps`（COM）—— 机器上装了并激活的 Office/WPS 时可用；
4. 全部不可用时返回 None，由调用方降级为 needs_review。

注意：COM 转换需要用户 profile 与 INetCache 可写，且在未激活的 Office 上会
直接拒绝调用，因此顺序上把 native_xls 放在 COM 之前。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from core.config import cfg
from core.workdir import work_root

_backend_cache: str | None = None
_backend_error: dict[str, str] = {}


def _soffice_exe() -> str | None:
    raw = os.environ.get("REDACT_SOFFICE") or cfg("convert", "soffice_path", default=None)
    if raw and Path(raw).exists():
        return str(raw)
    for name in ("soffice", "soffice.exe", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    for cand in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/bin/soffice",
    ):
        if Path(cand).exists():
            return cand
    return None


def _com_available() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        import win32com.client  # noqa: F401
    except Exception as e:  # noqa: BLE001
        _backend_error["com"] = f"pywin32 不可用: {type(e).__name__}"
        return False
    return True


def available_backend() -> str | None:
    """返回当前可用的转换后端名：soffice / native_xls / msoffice / wps / None。"""
    global _backend_cache
    if _backend_cache is not None:
        return _backend_cache or None
    forced = str(cfg("convert", "backend", default="auto") or "auto").lower()
    if forced == "none":
        _backend_cache = ""
        return None
    if forced in ("soffice", "libreoffice"):
        _backend_cache = "soffice" if _soffice_exe() else ""
        return _backend_cache or None
    if forced == "native_xls":
        _backend_cache = "native_xls"
        return _backend_cache
    if forced in ("msoffice", "wps"):
        _backend_cache = forced if _com_available() else ""
        return _backend_cache or None

    if _soffice_exe():
        _backend_cache = "soffice"
    else:
        _backend_cache = "native_xls"     # 无外部依赖，最优默认
    return _backend_cache or None


def backend_status() -> dict:
    return {
        "backend": available_backend(),
        "soffice": _soffice_exe(),
        "com": _com_available(),
        "errors": dict(_backend_error),
    }


# ---------------------------------------------------------------- native xls
def _convert_native_xls(src: Path, out_dir: Path) -> Path | None:
    """自写 BIFF8 读取器 -> xlsx（只保文本，够脱敏用）。

    额外兼容「假 .xls」：很多 ERP/扫描件导出的是**HTML 表格**却命名为 .xls
    （文件头是 `<html ...>`），BIFF 解析器会直接报错。这类文件按 HTML 解析。
    """
    from openpyxl import Workbook

    from handlers.xls_reader import OLEError, read_xls

    out = out_dir / (src.stem + ".xlsx")
    head = b""
    try:
        head = src.read_bytes()[:512].lstrip().lower()
    except OSError:
        pass

    sheets = None
    if head.startswith(b"<html") or head.startswith(b"<!doctype") or b"<table" in head:
        sheets = _read_html_tables(src)
        if sheets:
            _backend_error["native_xls"] = "HTML 伪装的 .xls，已按 HTML 表格解析"
    if not sheets:
        try:
            sheets = read_xls(src)
        except Exception as e:  # noqa: BLE001
            _backend_error["native_xls"] = f"{type(e).__name__}: {e}"
            return None
    if not sheets:
        _backend_error["native_xls"] = "未读到任何工作表"
        return None

    wb = Workbook()
    wb.remove(wb.active)
    for sh in sheets:
        title = (sh.name or "Sheet")[:31]
        for bad in '\\/*?:[]':
            title = title.replace(bad, "_")
        ws = wb.create_sheet(title=title or "Sheet")
        for row in sh.rows:
            ws.append([c if c != "" else None for c in row])
    wb.save(str(out))
    return out if out.exists() else None


def _read_html_tables(src: Path):
    """解析 HTML 伪装表格，返回 [Sheet]。"""
    from handlers.xls_reader import Sheet

    raw = src.read_bytes()
    text = None
    for enc in ("utf-8", "gb18030", "utf-16", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return []
    try:
        from lxml import html as lhtml
    except Exception:  # noqa: BLE001
        return []
    try:
        doc = lhtml.fromstring(text)
    except Exception:  # noqa: BLE001
        return []

    sheets = []
    for ti, table in enumerate(doc.xpath("//table")):
        rows = []
        for tr in table.xpath(".//tr"):
            cells = []
            for td in tr.xpath("./td|./th"):
                cells.append(" ".join(td.text_content().split()))
            if cells:
                rows.append(cells)
        if rows:
            sheets.append(Sheet(name=f"Sheet{ti + 1}", rows=rows))
    return sheets


# ---------------------------------------------------------------- soffice
def _convert_soffice(src: Path, out_dir: Path, target: str) -> Path | None:
    exe = _soffice_exe()
    if not exe:
        return None
    # 每次用独立 profile，避免与用户已打开的 LibreOffice 抢锁
    profile = work_root() / "lo_profile"
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe, "--headless", "--norestore", "--invisible",
        f"-env:UserInstallation=file:///{profile.as_posix()}",
        "--convert-to", target, "--outdir", str(out_dir), str(src),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180)
    except Exception as e:  # noqa: BLE001
        _backend_error["soffice"] = f"{type(e).__name__}: {e}"
        return None
    out = out_dir / (src.stem + "." + target.split(":")[0])
    if out.exists():
        return out
    _backend_error["soffice"] = (proc.stderr or b"").decode("utf-8", "ignore")[:400]
    return None


# ---------------------------------------------------------------- Office COM
_EXCEL_FMT_XLSX = 51
_WORD_FMT_DOCX = 16


def _convert_com(src: Path, out_dir: Path, target: str) -> Path | None:
    """用 Office/WPS COM 转换。

    注意：COM 是「单实例单线程」的，批量循环里连续 DispatchEx 容易出现
    `被呼叫方拒绝接收呼叫 (-2147418111)`（上一个实例还没退干净）。
    这里加了重试 + 先清理残留进程 + `CoInitialize`，并且每次转换后强制 Quit。
    """
    if not _com_available():
        return None
    import time

    import pythoncom
    import win32com.client as win32

    out = out_dir / (src.stem + "." + target)
    prog_ids = ("Excel.Application", "KET.Application") if target == "xlsx" else (
        "Word.Application", "KWPS.Application"
    )
    last_err = ""
    for attempt in range(3):
        pythoncom.CoInitialize()
        app = None
        try:
            for prog in prog_ids:
                try:
                    app = win32.DispatchEx(prog)
                    break
                except Exception:  # noqa: BLE001
                    continue
            if app is None:
                _backend_error["com"] = "Office/WPS COM 均不可用"
                return None
            try:
                app.Visible = False
            except Exception:  # noqa: BLE001
                pass
            try:
                app.DisplayAlerts = False
            except Exception:  # noqa: BLE001
                pass

            if target == "xlsx":
                wb = app.Workbooks.Open(str(src), UpdateLinks=0, ReadOnly=True)
                try:
                    wb.SaveAs(str(out), FileFormat=_EXCEL_FMT_XLSX)
                finally:
                    wb.Close(False)
            else:
                doc = app.Documents.Open(str(src), ReadOnly=True, AddToRecentFiles=False)
                try:
                    doc.SaveAs2(str(out), FileFormat=_WORD_FMT_DOCX)
                finally:
                    doc.Close(False)
            if out.exists():
                return out
            last_err = "转换后文件不存在"
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {str(e)[:200]}"
            _backend_error["com"] = last_err
        finally:
            if app is not None:
                try:
                    app.Quit()
                except Exception:  # noqa: BLE001
                    pass
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.6 + attempt * 0.8)
    return None


def convert_legacy(src, out_dir, target: str) -> Path | None:
    """把 .doc/.xls/.et 转成 target（docx/xlsx）。返回新路径或 None。"""
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    backend = available_backend()

    if target == "xlsx":
        if backend == "soffice":
            got = _convert_soffice(src, out_dir, target)
            if got:
                return got
        got = _convert_native_xls(src, out_dir)
        if got:
            if backend != "soffice":
                _backend_error["note"] = "xls 由内置 BIFF 读取器转换（格式会简化，文字完整保留）"
            return got
        if _com_available():
            return _convert_com(src, out_dir, target)
        return None

    if target == "docx":
        if backend == "soffice":
            got = _convert_soffice(src, out_dir, target)
            if got:
                return got
        if _com_available():
            got = _convert_com(src, out_dir, target)
            if got:
                return got
        _backend_error["doc"] = (
            "缺少 LibreOffice，且 Office/WPS COM 不可用（目标机 Office 未激活），"
            ".doc 无法自动转换"
        )
        return None
    return None
