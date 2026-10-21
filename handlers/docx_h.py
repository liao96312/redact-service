"""T2.1 docx 处理器：zipfile + lxml，不用 python-docx。

流程：
1. 解压，处理 document.xml / header*.xml / footer*.xml / footnotes / endnotes / comments
2. 清理修订：删除 w:del，展开 w:ins
3. 每个 w:p 拼接直属文字节点后识别并替换
4. word/media/* 图片走 T2.4
5. 清空 docProps 的 creator / lastModifiedBy / Company
6. 重新打包

真实图纸语料暴露并修复的问题：
- `./w:r/w:t | ./w:hyperlink/w:r/w:t` 漏掉了 `w:sdt`（内容控件）、`w:smartTag`、
  `w:ins` 展开后的嵌套 run、以及表格单元格里的 run（某些 Word 版本会包一层
  sdt），导致报价单里「客户地址」被整段漏检。改为遍历段落内**所有** w:t，
  只要它不在 w:del 里；
- 只处理了 document.xml 等 6 个部件，`word/charts`、`word/embeddings`、
  `docProps/custom.xml` 里的文字没处理；这里补 custom.xml 与 chart 的标题缓存；
- 重新打包时用原 ZipInfo，保留压缩方式与目录项；旧实现 `zout.writestr(name, ...)`
  会丢掉目录条目权限位，个别压缩工具打开会报错。
"""
from __future__ import annotations

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
CUSTOM = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"

_CONTENT_RE = re.compile(
    r"^word/(document\.xml|header\d*\.xml|footer\d*\.xml|footnotes\.xml|endnotes\.xml|comments\.xml)$"
)
_CHART_RE = re.compile(r"^word/charts/chart\d*\.xml$")

# 需要清空的文档属性（作者/公司/最后修改人/标题/主题/关键词）
_CLEAR_TAGS = [
    f"{{{DC}}}creator",
    f"{{{DC}}}title",
    f"{{{DC}}}subject",
    f"{{{DC}}}description",
    f"{{{DC}}}keywords",
    f"{{{CP}}}lastModifiedBy",
    f"{{{EXT}}}Company",
    f"{{{EXT}}}Manager",
    f"{{{EXT}}}Application",
]


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


def _paragraph_text_nodes(p):
    """段落内全部 w:t（含 sdt / smartTag / hyperlink / 表格 里的 run），排除 w:del。"""
    out = []
    for t in p.iter(f"{{{W}}}t"):
        # 跳过已被删除的修订内容
        anc = t.getparent()
        deleted = False
        while anc is not None:
            if anc.tag == f"{{{W}}}del":
                deleted = True
                break
            anc = anc.getparent()
        if not deleted:
            out.append(t)
    return out


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
    parser = etree.XMLParser(remove_blank_text=False, huge_tree=True, resolve_entities=False)
    root = etree.fromstring(data, parser=parser)
    _clean_revisions(root)
    for p in root.iter(f"{{{W}}}p"):
        ts = _paragraph_text_nodes(p)
        if ts:
            _mask_paragraph(ts, detect, rep)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _process_chart(data: bytes, detect, rep: Report) -> bytes:
    """图表缓存里的分类名/系列名也可能含客户名，按文本节点整体处理。"""
    parser = etree.XMLParser(remove_blank_text=False, huge_tree=True, resolve_entities=False)
    root = etree.fromstring(data, parser=parser)
    for tag in ("{http://schemas.openxmlformats.org/drawingml/2006/chart}t"):
        for el in root.iter(tag):
            txt = el.text or ""
            if not txt.strip():
                continue
            spans = detect(txt)
            if spans:
                rep.add_spans(spans)
                el.text = _apply(txt, spans)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _apply(text: str, spans) -> str:
    out = text
    for s in sorted(spans, key=lambda x: x.start, reverse=True):
        out = out[: s.start] + placeholder(s.type) + out[s.end:]
    return out


def _clear_docprops(data: bytes, detect=None, rep: Report | None = None) -> bytes:
    root = etree.fromstring(data)
    for tag in _CLEAR_TAGS:
        for el in root.iter(tag):
            el.text = ""
    # 自定义属性里的值也要过一遍检测（常见「项目负责人=张三」）
    if detect is not None:
        for el in root.iter():
            if not isinstance(el.tag, str) or not el.text or not el.text.strip():
                continue
            if el.tag.startswith(f"{{{CUSTOM}}}"):
                spans = detect(el.text)
                if spans:
                    if rep is not None:
                        rep.add_spans(spans)
                    el.text = _apply(el.text, spans)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def redact_docx(in_path, out_path, detect) -> Report:
    in_path, out_path = Path(in_path), Path(out_path)
    rep = Report()
    try:
        with zipfile.ZipFile(in_path, "r") as zin:
            infos = zin.infolist()
            contents = {i.filename: zin.read(i.filename) for i in infos}
    except zipfile.BadZipFile:
        return rep.fail("不是有效的 docx（zip 损坏）")

    if "word/document.xml" not in contents:
        return rep.fail("缺少 word/document.xml，可能不是 docx")

    for info in infos:
        name = info.filename
        if _CONTENT_RE.match(name):
            try:
                contents[name] = _process_content(contents[name], detect, rep)
            except etree.XMLSyntaxError:
                rep.warn(f"XML 解析失败: {name}", review=True)
        elif _CHART_RE.match(name):
            try:
                contents[name] = _process_chart(contents[name], detect, rep)
            except etree.XMLSyntaxError:
                rep.warn(f"图表 XML 解析失败: {name}")

    # 图片
    from handlers.media import redact_media_bytes

    for name in list(contents):
        if name.startswith("word/media/") and not name.endswith("/"):
            contents[name] = redact_media_bytes(name, contents[name], detect, rep)

    # docProps
    for name in ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"):
        if name in contents:
            try:
                contents[name] = _clear_docprops(contents[name], detect, rep)
            except etree.XMLSyntaxError:
                pass

    _write_zip(out_path, infos, contents)
    return rep


def _write_zip(out_path: Path, infos, contents: dict[str, bytes]) -> None:
    with zipfile.ZipFile(out_path, "w") as zout:
        for info in infos:
            clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            clone.compress_type = info.compress_type
            clone.external_attr = info.external_attr
            data = contents.get(info.filename, b"")
            zout.writestr(clone, data)
