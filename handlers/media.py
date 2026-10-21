"""内嵌图片脱敏公共工具（docx / xlsx / pptx 共用）。

修正点：
- 旧实现在 `with tempfile.TemporaryDirectory()` 内部处理，临时目录清理异常
  （Windows 上杀软/漫游配置常见）会被外层 broad except 当成「图片处理失败」，
  实测一份 xlsx 里 31 张图全部被误报为失败并整体置 needs_review。
  改为：处理与结果读取放在 try 内，清理放在 finally 且忽略错误；
- 支持 `REDACT_WORK_DIR` / 项目内 `.work/`，不再依赖系统临时目录；
- 图片未发生变化时直接返回原始字节，避免无谓的体积/质量损失；
- 同一条警告只记一次，避免刷屏。
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from handlers.base import Report
from core.workdir import temporary_workdir

_IMG_EXT = ("png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp")
_MEDIA_PREFIXES = ("word/media/", "xl/media/", "ppt/media/")


def work_dir() -> Path:
    """兼容旧调用：返回工作根目录。"""
    from core.workdir import work_root

    return work_root()


def redact_media_bytes(name: str, data: bytes, detect, rep: Report) -> bytes:
    ext = name.rsplit(".", 1)[-1].lower()
    if ext not in _IMG_EXT or not data:
        return data
    try:
        from handlers.image_h import redact_image
    except Exception as e:  # noqa: BLE001
        rep.warn(f"内嵌图片处理不可用: {type(e).__name__}")
        return data

    out: bytes | None = None
    try:
        with temporary_workdir("media") as td:
            ip = td / f"in.{ext}"
            op = td / f"out.{ext}"
            ip.write_bytes(data)
            r = redact_image(ip, op, detect)
            rep.warnings.extend(r.warnings)
            if r.needs_review:
                rep.needs_review = True
                if rep.status == "done":
                    rep.status = "needs_review"
            for k, v in r.counts.items():
                rep.add(k, v)
            if r.status == "failed":
                rep.warn(f"内嵌图片处理失败({name}): {r.message or 'unknown'}", review=True)
                return data
            out = op.read_bytes() if op.exists() else data
    except Exception as e:  # noqa: BLE001
        rep.warn(f"内嵌图片处理失败({name}): {type(e).__name__}", review=True)
        return data

    # 内容未变化时保留原始字节，避免重新编码带来的画质与体积损失
    if out and hashlib.sha256(out).digest() == hashlib.sha256(data).digest():
        return data
    return out if out else data


def redact_media_in_zip(path, detect, rep: Report) -> None:
    """就地对 zip 文件中的 media 图片脱敏（重打包），保留原压缩方式与顺序。"""
    path = Path(path)
    with zipfile.ZipFile(path, "r") as zin:
        infos = zin.infolist()
        contents = {i.filename: zin.read(i.filename) for i in infos}

    for info in infos:
        if info.filename.startswith(_MEDIA_PREFIXES):
            contents[info.filename] = redact_media_bytes(
                info.filename, contents[info.filename], detect, rep
            )

    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in infos:
            clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            clone.compress_type = info.compress_type
            clone.external_attr = info.external_attr
            zout.writestr(clone, contents[info.filename])
    tmp.replace(path)
