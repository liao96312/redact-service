"""T2.8 pptx 处理器：zipfile + lxml。

图纸包里的 3D 效果图大量是 pptx（最大 100MB），原来的 dispatch 直接
`return rep.fail("暂不支持 pptx")`，整包只能人工处理。这里补上处理器：

- 遍历 `ppt/slides/slideN.xml` 的 `<a:t>` 文本节点，逐「段落」（a:p）拼接后检测；
- 处理 `ppt/notesSlides/*.xml`（备注里常有客户名与电话）；
- 处理 `ppt/charts/*.xml` 的图表标题与分类名；
- `ppt/media/*` 图片走图片脱敏；
- 清空 `docProps/core.xml` / `app.xml` 的作者与公司信息；
- 重新打包时保留原 ZipInfo（压缩方式、目录项）。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from lxml import etree

from core.config import placeholder
from handlers.base import Report

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
DC = "http://purl.org/dc/elements/1.1/"
CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
EXT = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"

_SLIDE_RE = re.compile(r"^ppt/(slides|notesSlides|slideLayouts|slideMasters)/[^/]+\.xml$")
_CHART_RE = re.compile(r"^ppt/charts/[^/]+\.xml$")

_CLEAR_TAGS = [
    f"{{{DC}}}creator",
    f"{{{DC}}}lastModifiedBy",
    f"{{{DC}}}title",
    f"{{{DC}}}subject",
    f"{{{CP}}}lastModifiedBy",
    f"{{{EXT}}}Company",
    f"{{{EXT}}}Manager",
]


def _apply(text: str, spans) -> str:
    out = text
    for s in sorted(spans, key=lambda x: x.start, reverse=True):
        out = out[: s.start] + placeholder(s.type) + out[s.end:]
    return out


def _mask_paragraph(p, detect, rep: Report) -> None:
    ts = [t for t in p.iter(f"{{{A}}}t")]
    if not ts:
        return
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
            first = False


def _process_xml(data: bytes, detect, rep: Report, kind: str) -> bytes:
    parser = etree.XMLParser(remove_blank_text=False, huge_tree=True, resolve_entities=False)
    root = etree.fromstring(data, parser=parser)
    if kind == "chart":
        for tag in (f"{{{C}}}v",):
            for el in root.iter(tag):
                txt = el.text or ""
                if not txt.strip():
                    continue
                spans = detect(txt)
                if spans:
                    rep.add_spans(spans)
                    el.text = _apply(txt, spans)
    else:
        for p in root.iter(f"{{{A}}}p"):
            _mask_paragraph(p, detect, rep)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _clear_docprops(data: bytes) -> bytes:
    root = etree.fromstring(data)
    for tag in _CLEAR_TAGS:
        for el in root.iter(tag):
            el.text = ""
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def redact_pptx(in_path, out_path, detect) -> Report:
    in_path, out_path = Path(in_path), Path(out_path)
    rep = Report()
    try:
        with zipfile.ZipFile(in_path, "r") as zin:
            infos = zin.infolist()
            contents = {i.filename: zin.read(i.filename) for i in infos}
    except zipfile.BadZipFile:
        return rep.fail("不是有效的 pptx（zip 损坏）")

    if not any(n.startswith("ppt/") for n in contents):
        return rep.fail("缺少 ppt/ 部件，可能不是 pptx")

    for info in infos:
        name = info.filename
        if _SLIDE_RE.match(name):
            try:
                contents[name] = _process_xml(contents[name], detect, rep, "slide")
            except etree.XMLSyntaxError:
                rep.warn(f"XML 解析失败: {name}", review=True)
        elif _CHART_RE.match(name):
            try:
                contents[name] = _process_xml(contents[name], detect, rep, "chart")
            except etree.XMLSyntaxError:
                rep.warn(f"图表 XML 解析失败: {name}")

    from handlers.media import redact_media_bytes

    for name in list(contents):
        if name.startswith("ppt/media/") and not name.endswith("/"):
            contents[name] = redact_media_bytes(name, contents[name], detect, rep)

    for name in ("docProps/core.xml", "docProps/app.xml"):
        if name in contents:
            try:
                contents[name] = _clear_docprops(contents[name])
            except etree.XMLSyntaxError:
                pass

    with zipfile.ZipFile(out_path, "w") as zout:
        for info in infos:
            clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            clone.compress_type = info.compress_type
            clone.external_attr = info.external_attr
            zout.writestr(clone, contents.get(info.filename, b""))
    return rep
