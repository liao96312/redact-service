"""T2.2 xlsx 处理器（openpyxl）。

- 遍历全部 sheet（含隐藏），字符串单元格走 detect
- 表头规则：前 5 行命中关键词的列，整列替换为占位符
- 删除批注；清空 creator / lastModifiedBy；sheet 名命中则改名
- 公式含命中内容 -> warning + needs_review
- 含图表 -> needs_review；内嵌图片走 T2.4
- .xls 已由 dispatch 转成 .xlsx
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from core.config import placeholder
from handlers.base import Report
from handlers.media import redact_media_in_zip

HEADER_KEYWORDS = {
    "PERSON": ["姓名", "客户", "联系人", "户名", "name"],
    "PHONE": ["电话", "手机", "phone", "mobile", "tel"],
    "BANK_CARD": ["银行卡", "卡号", "账号", "account", "card"],
    "ORG": ["公司", "单位", "company", "org"],
    "ADDRESS": ["地址", "address"],
    "EMAIL": ["邮箱", "邮件", "email", "e-mail"],
}
_HEADER_ROWS = 5


def _match_header(text: str) -> str | None:
    low = text.lower()
    for typ, kws in HEADER_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in low:
                return typ
    return None


def redact_xlsx(in_path, out_path, detect) -> Report:
    in_path, out_path = Path(in_path), Path(out_path)
    rep = Report()
    try:
        wb = load_workbook(str(in_path), data_only=False)
    except Exception as e:
        return rep.fail(f"无法打开 xlsx: {type(e).__name__}")

    renamed = 0
    for ws in list(wb.worksheets):
        # 表头列
        header_cols: dict[int, tuple[str, int]] = {}
        for row in ws.iter_rows(min_row=1, max_row=min(_HEADER_ROWS, ws.max_row or 1)):
            for cell in row:
                if isinstance(cell.value, str):
                    typ = _match_header(cell.value)
                    if typ and cell.column not in header_cols:
                        header_cols[cell.column] = (typ, cell.row)
        # 图表
        if getattr(ws, "_charts", None):
            rep.warn(f"sheet《{ws.title}》含图表，保存可能丢失", review=True)

        for row in ws.iter_rows():
            for cell in row:
                # 删除批注
                if cell.comment is not None:
                    cell.comment = None
                v = cell.value
                if v is None:
                    continue
                # 公式
                if isinstance(v, str) and v.startswith("="):
                    if detect(v):
                        rep.warn(f"公式 {cell.coordinate} 含疑似敏感内容", review=True)
                    continue
                if not isinstance(v, str):
                    continue
                hc = header_cols.get(cell.column)
                if hc and cell.row > hc[1]:
                    rep.add(hc[0])
                    cell.value = placeholder(hc[0])
                    continue
                spans = detect(v)
                if spans:
                    rep.add_spans(spans)
                    cell.value = _apply(v, spans)

        # sheet 名
        if detect(ws.title):
            renamed += 1
            rep.warn("sheet 名命中敏感信息，已改名")
            ws.title = f"Sheet{renamed}"

    # 属性清空
    try:
        wb.properties.creator = ""
        wb.properties.lastModifiedBy = ""
        wb.properties.title = ""
    except Exception:
        pass

    wb.save(str(out_path))
    redact_media_in_zip(out_path, detect, rep)
    return rep


def _apply(text: str, spans) -> str:
    out = text
    for s in sorted(spans, key=lambda x: x.start, reverse=True):
        out = out[: s.start] + placeholder(s.type) + out[s.end:]
    return out
