"""T2.1 docx 处理器：zipfile + lxml，不用 python-docx。

流程：
1. 解压，处理 document.xml / header*.xml / footer*.xml / footnotes / endnotes / comments
2. 清理修订：删除 w:del，展开 w:ins
3. 每个 w:p 拼接直属文字节点后识别并替换
4. word/media/* 图片走 T2.4
5. 清空 docProps 的 creator / lastModifiedBy / Company
6. 重新打包
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from lxml import etree

from core.config import placeholder
from handlers.base import Report

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML = "http://www.w3.org/XML/1998/namespace"
NS = {"w": W}

DC = "http://purl.org/dc/elements/1.1/"
CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
EXT = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"

_CONTENT_RE = re.compile(
    r"^word/(document\.xml|header\d*\.xml|footer\d*\.xml|footnotes\.xml|endnotes\.xml|comments\.xml)$"
)


def _clean_revisions(root) -> None:
    for el in list(root.iter(f"{{{W}}}del")):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)
    for el in list(root.iter(f"{{{W}}}ins")):
        parent = el.getparent()
        if parent is None:
            continue
        idx = list(parent).index(el)
        for child in list(el):
            parent.insert(idx, child)
            idx += 1
        parent.remove(el)


def _mask_paragraph(ts, detect, rep) -> None:
    full = "".join(t.text or "" for t in ts)
    if not full.strip():
        return
    spans = detect(full)
    if not spans:
        return
    rep.add_spans(spans)
    for sp in sorted(spans, key=lambda s: s.start, reverse=True):
        pos, first = 0, True
        for t in ts:
            txt = t.text or ""
            a0, a1 = pos, pos + len(txt)
            pos = a1
            if a1 <= sp.start or a0 >= sp.end:
                continue
            i, j = max(sp.start, a0) - a0, min(sp.end, a1) - a0
            t.text = txt[:i] + (placeholder(sp.type) if first else "") + txt[j:]
            t.set(f"{{{XML}}}space", "preserve")
            first = False


def _process_content(data: bytes, detect, rep: Report) -> bytes:
    parser = etree.XMLParser(remove_blank_text=False, huge_tree=True)
    root = etree.fromstring(data, parser=parser)
    _clean_revisions(root)
    for p in root.iter(f"{{{W}}}p"):
        ts = p.xpath("./w:r/w:t | ./w:hyperlink/w:r/w:t", namespaces=NS)
        if ts:
            _mask_paragraph(ts, detect, rep)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _clear_docprops(data: bytes) -> bytes:
    root = etree.fromstring(data)
    for tag in (f"{{{DC}}}creator", f"{{{CP}}}lastModifiedBy"):
        for el in root.iter(tag):
            el.text = ""
    for el in root.iter(f"{{{EXT}}}Company"):
        el.text = ""
    # docProps/app.xml
    for el in root.iter(f"{{{EXT}}}Manager"):
        el.text = ""
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def redact_docx(in_path, out_path, detect) -> Report:
    in_path, out_path = Path(in_path), Path(out_path)
    rep = Report()
    try:
        with zipfile.ZipFile(in_path, "r") as zin:
            names = zin.namelist()
            contents = {n: zin.read(n) for n in names}
    except zipfile.BadZipFile:
        return rep.fail("不是有效的 docx（zip 损坏）")

    if "word/document.xml" not in contents:
        return rep.fail("缺少 word/document.xml，可能不是 docx")

    # 修订/隐藏文字提示
    for name in list(contents):
        if _CONTENT_RE.match(name):
            try:
                if b"<w:del " in contents[name] or b"<w:ins " in contents[name]:
                    contents[name] = _process_content(contents[name], detect, rep)
                else:
                    contents[name] = _process_content(contents[name], detect, rep)
            except etree.XMLSyntaxError:
                rep.warn(f"XML 解析失败: {name}", review=True)

    # 图片
    for name in list(contents):
        if name.startswith("word/media/"):
            contents[name] = _redact_media(name, contents[name], detect, rep)

    # docProps
    for name in ("docProps/core.xml", "docProps/app.xml"):
        if name in contents:
            try:
                contents[name] = _clear_docprops(contents[name])
            except etree.XMLSyntaxError:
                pass

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in names:
            zout.writestr(name, contents[name])
    return rep


def _redact_media(name: str, data: bytes, detect, rep: Report) -> bytes:
    ext = name.rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp"):
        return data
    try:
        import tempfile

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
