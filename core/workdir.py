"""临时工作目录工具。

项目原来到处用 `tempfile.TemporaryDirectory()` / `mkdtemp()`。在受限环境
（Windows 沙箱 ACL、企业漫游配置、杀软实时扫描）下，mkdtemp 会创建 0o700
的目录并带上拒绝写入的 ACL，导致后续 `write_bytes` 直接 PermissionError。
实测同一路径用 `Path.mkdir()` 创建就完全正常，因此这里统一用 mkdir + 随机名。

用法：
    with make_workdir("media") as d:
        ...
"""
from __future__ import annotations

import contextlib
import os
import shutil
import uuid
from pathlib import Path

from core.config import ROOT, cfg


def work_root() -> Path:
    """返回可写的工作根目录：优先 REDACT_WORK_DIR，其次配置，最后项目内 .work/。"""
    raw = os.environ.get("REDACT_WORK_DIR") or cfg("paths", "work_dir", default=None)
    candidates = [Path(raw)] if raw else []
    candidates.append(ROOT / ".work")
    candidates.append(Path(os.environ.get("TEMP") or os.environ.get("TMP") or "."))
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / f".probe_{uuid.uuid4().hex[:8]}"
            probe.mkdir()
            (probe / "x").write_bytes(b"1")
            shutil.rmtree(probe, ignore_errors=True)
            return base
        except OSError:
            continue
    raise OSError("找不到可写的临时目录")


def make_workdir(prefix: str = "work") -> Path:
    base = work_root()
    for _ in range(16):
        d = base / f"{prefix}_{uuid.uuid4().hex[:12]}"
        try:
            d.mkdir()
            return d
        except FileExistsError:
            continue
    raise OSError("无法创建临时目录")


@contextlib.contextmanager
def temporary_workdir(prefix: str = "work"):
    d = make_workdir(prefix)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
