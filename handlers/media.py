"""内嵌图片脱敏公共工具（docx / xlsx 共用）。"""
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from handlers.base import Report

_IMG_EXT = ("png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp")
_MEDIA_PREFIXES = ("word/media/", "xl/media/", "ppt/media/")


def redact_media_bytes(name: str, data: bytes, detect, rep: Report) -> bytes:
    ext = name.rsplit(".", 1)[-1].lower()
    if ext not in _IMG_EXT:
        return data
    try:
        from handlers.image_h import redact_image

        with tempfile.TemporaryDirectory() as td:
            ip = Path(td) / f"in.{ext}"
            op = Path(td) / f"out.{ext}"
            ip.write_bytes(data)
            r = redact_image(ip, op, detect)
            rep.warnings.extend(r.warnings)
            if r.needs_review:
                rep.needs_review = True
            for k, v in r.counts.items():
                rep.add(k, v)
            return op.read_bytes()
    except Exception as e:  # pragma: no cover
        rep.warn(f"内嵌图片处理失败({name}): {type(e).__name__}", review=True)
        return data


def redact_media_in_zip(path, detect, rep: Report) -> None:
    """就地对 zip 文件中的 media 图片脱敏（重打包）。"""
    path = Path(path)
    with zipfile.ZipFile(path, "r") as zin:
        names = zin.namelist()
        contents = {n: zin.read(n) for n in names}
    for name in names:
        if name.startswith(_MEDIA_PREFIXES):
            contents[name] = redact_media_bytes(name, contents[name], detect, rep)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in names:
            zout.writestr(name, contents[name])
