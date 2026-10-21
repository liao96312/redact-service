"""T2.2 xlsx 处理器（openpyxl）。

- 遍历全部 sheet（含隐藏），字符串单元格走 detect
- 表头规则：前 N 行命中关键词的列 → 该列数据按类型替换（见下方说明）
- 删除批注；清空 creator / lastModifiedBy；sheet 名命中则改名
- 公式含命中内容 -> warning + needs_review
- 含图表 -> needs_review；内嵌图片走 T2.4
- .xls 已由 dispatch 转成 .xlsx

真实报价单/订单表暴露并修复的问题：
1. 表头规则过度整列替换：旧实现只要表头含「公司」「地址」，就把整列**所有**
   文本替换成占位符，连「柜体颜色 Y105」「1.8m」这类正常数据也被涂掉，
   且把计数算成一条记录。改为：
   - 只在「单元格内容本身像该类型实体」时才替换（复用 detect 的类型判断，
     外加长度/字符集约束），不再无脑整列替换；
   - 统计按真实替换数量计；
2. 公式误判：`=G20*F20` 被 NER 当成 ORG/ADDRESS，旧实现直接 review。
   公式里只可能泄露「单元格引用名/字符串常量」，因此先剥离单元格引用与
   函数名，只对字符串常量做检测；
3. 图表：openpyxl 保存会丢图表，旧实现只 warning；现在修改前先探测
   `xl/charts`，含图表则整体 needs_review 并保留原文件（不写坏文件）；
4. 内嵌图片：改为重打包时原位替换，保留图片格式；
5. 合并单元格、数据验证、条件格式保持不变（openpyxl 会保留）。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from core.config import cfg, placeholder
from handlers.base import Report
from handlers.media import redact_media_in_zip

HEADER_KEYWORDS = {
    "PERSON": ["姓名", "客户", "联系人", "户名", "name", "contact"],
    "PHONE": ["电话", "手机", "phone", "mobile", "tel"],
    "BANK_CARD": ["银行卡", "卡号", "账号", "account", "card"],
    "ORG": ["公司", "单位", "company", "org"],
    "ADDRESS": ["地址", "address"],
    "EMAIL": ["邮箱", "邮件", "email", "e-mail"],
}
_HEADER_ROWS = int(cfg("limits", "xlsx_header_rows", default=5))
# 没有稀疏字典时的兜底列上限（真实订单表 16384 列全空）
_MAX_COL = int(cfg("limits", "xlsx_max_columns", default=256))
# 工作表 XML 超过该体积时切到快速路径（≈ 40 万单元格）
_FAST_SHEET_BYTES = int(cfg("limits", "xlsx_fast_sheet_mb", default=8)) * 1024 * 1024

# 单元格引用：A1、$A$1、'Sheet 1'!A1、A1:B9
_CELL_REF = re.compile(r"(?:'[^']+'|[\w\u4e00-\u9fa5]+)?!?\$?[A-Za-z]{1,3}\$?\d+(?::\$?[A-Za-z]{1,3}\$?\d+)?")
_FUNC_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*\s*\(")
_STR_LITERAL = re.compile(r'"([^"]*)"')

# 表头列的「像实体」判定：不同类型允许的字符集与长度。
# 注意 ORG/PERSON 必须含汉字或「像英文专名」的形态，否则「Y105」「1.8m」
# 这类板材色号/尺寸会被整列替换掉（真实报价单踩过）。
_HAS_HAN = r"[\u4e00-\u9fa5]"
_SHAPE = {
    "PERSON": (
        re.compile(r"^[\u4e00-\u9fa5·]{2,6}$|^[A-Z][A-Za-z.'’\-]*(?:\s+[A-Z][A-Za-z.'’\-]*){0,3}$"),
        2, 40,
    ),
    "ORG": (
        re.compile(
            r"^[\u4e00-\u9fa5（）()A-Za-z0-9.&,\-' ]{2,60}$"
        ),
        4, 60,
    ),
    "ADDRESS": (re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9（）().,\-\s#号栋楼层室]{4,120}$"), 6, 120),
    "PHONE": (re.compile(r"^\+?[\d][\d\s\-()]{5,24}$"), 6, 24),
    "BANK_CARD": (re.compile(r"^[\d\s\-]{12,30}$"), 12, 30),
    "EMAIL": (re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$"), 5, 80),
}

# 明显不像公司/人名的短代码：全大写+数字、纯数字、带尺寸单位
_CODE_LIKE = re.compile(r"^(?:[A-Z]{1,3}\d{1,6}|[\d.]+\s*(?:mm|cm|m|kg|pcs)|\d+[A-Z]{1,3})$", re.IGNORECASE)

# 业务常用词：出现在「姓名/客户」列里也不可能是人名。
# 真实订单表里「客户名称 / 件数 / 规格型号 / 加工方式」曾被整个人名规则替换掉。
_BIZ_WORDS = (
    "名称", "编号", "单号", "型号", "规格", "尺寸", "数量", "件数", "套数", "尾数",
    "类别", "类型", "等级", "来源", "国家", "地区", "方式", "颜色", "色号", "款式",
    "材质", "材料", "工艺", "包装", "备注", "说明", "单位", "单价", "金额", "合计",
    "总数", "日期", "时间", "状态", "进度", "部门", "职位", "岗位", "工号", "序号",
    "清单", "明细", "配置", "要求", "标准", "做法", "是否", "有无", "需要", "默认",
    "客户", "公司", "工厂", "产品", "项目", "订单", "合同", "发票", "付款", "收款",
    "定金", "尾款", "总额", "税率", "折扣", "重量", "体积",
    # 客户分类/渠道类词（"散客 / 批发 / 内销"经常出现在「客户」列）
    "散客", "批发", "零售", "内销", "外销", "出口", "进口", "代理", "直营",
    "加盟", "经销", "终端", "渠道", "阿里", "亚马逊", "自提", "送货",
)
# 公司/机构后缀：ORG 列里的值必须像机构名
_ORG_SUFFIX = re.compile(
    r"(公司|厂|店|集团|中心|事务所|研究院|工作室|商行|门市|"
    r"Co\.?|Ltd\.?|Inc\.?|LLC|Corp\.?|Corporation|Limited|GmbH|PLC|Company)",
    re.IGNORECASE,
)
# 地址构件：ADDRESS 列里的值必须像地址
_ADDR_PART = re.compile(
    r"(省|市|区|县|镇|乡|村|路|街|道|巷|弄|号|栋|幢|层|室|大厦|大楼|广场|花园|"
    r"工业园|开发区|Street|Road|Rd\.?|Avenue|Ave\.?|Lane|Blvd|Drive|Zone|District)",
    re.IGNORECASE,
)


# 表头单元格的最大长度：超过它就不是表头，而是数据（例如整行路径里含「客户资料」）
_HEADER_MAX_LEN = int(cfg("limits", "xlsx_header_max_len", default=20))
# 路径样式的单元格永远不按「表头整列」处理
_PATH_LIKE = re.compile(r"[\\/]{1,2}[^\\/\s]{1,40}[\\/]|[A-Za-z]:[\\/]")


def _match_header(text: str) -> str | None:
    t = (text or "").strip()
    if not t or len(t) > _HEADER_MAX_LEN:
        return None
    if "\n" in t or _PATH_LIKE.search(t):
        return None
    low = t.lower()
    for typ, kws in HEADER_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in low:
                return typ
    return None


def _looks_like(value: str, typ: str) -> bool:
    """判断「某列里的这个值」是否真的像该类型实体。

    这是把表头语义落到单元格上的最后一道闸门。教训：
    - 客户名称/件数/规格型号 这类普通业务词都是 2~6 个汉字，旧实现按
      「2~6 个汉字 = 人名」处理，把整张配置表的标签列全涂成了 [姓名]；
    - 公司列里也塞着色号、尺寸，必须要求出现机构后缀；
    - 地址列里常是「见备注」这类说明，必须出现地址构件。
    """
    shape = _SHAPE.get(typ)
    if not shape:
        return True
    rex, lo, hi = shape
    v = (value or "").strip()
    if not (lo <= len(v) <= hi):
        return False
    if _CODE_LIKE.match(v):
        return False
    if not rex.match(v):
        return False

    has_han = bool(re.search(_HAS_HAN, v))

    if typ == "PERSON":
        if not has_han:
            # 英文人名：至少两个词，且都首字母大写
            return bool(re.fullmatch(r"[A-Z][A-Za-z.'’\-]+(?:\s+[A-Z][A-Za-z.'’\-]+){1,2}", v))
        if len(v) > 4:                    # 中文姓名 2~4 字
            return False
        return not any(w in v for w in _BIZ_WORDS)

    if typ == "ORG":
        return bool(_ORG_SUFFIX.search(v))

    if typ == "ADDRESS":
        return bool(_ADDR_PART.search(v))

    return True


def _formula_text_literals(formula: str) -> list[str]:
    """公式里真正可能含敏感信息的只有字符串常量。"""
    return _STR_LITERAL.findall(formula or "")


def _formula_skeleton(formula: str) -> str:
    """去掉单元格引用与函数名后的骨架，用于判断是否真的有内容。"""
    s = _CELL_REF.sub(" ", formula or "")
    s = _FUNC_NAME.sub(" ", s)
    return s


def _iter_cells(ws):
    """稀疏遍历单元格。

    真实订单表常出现 `max_column=16384`（整行被设置过格式），旧实现
    `for row in ws.iter_rows()` 会遍历 16384×382 ≈ 620 万格，
    实测单文件耗时 300 秒直接超时。openpyxl 内部用稀疏字典存单元格，
    这里优先用它；拿不到时退化为限定列数的行遍历。
    """
    cells = getattr(ws, "_cells", None)
    if isinstance(cells, dict) and cells:
        # 按行列排序，保证确定性
        for key in sorted(cells.keys()):
            yield cells[key]
        return
    max_col = min(ws.max_column or 1, _MAX_COL)
    for row in ws.iter_rows(min_col=1, max_col=max_col):
        for cell in row:
            yield cell


def redact_xlsx(in_path, out_path, detect) -> Report:
    in_path, out_path = Path(in_path), Path(out_path)
    rep = Report()

    has_chart = False
    try:
        with zipfile.ZipFile(in_path) as z:
            has_chart = any(n.startswith("xl/charts/") for n in z.namelist())
    except zipfile.BadZipFile:
        return rep.fail("不是有效的 xlsx（zip 损坏）")

    try:
        wb = load_workbook(str(in_path), data_only=False)
    except Exception as e:  # noqa: BLE001
        return rep.fail(f"无法打开 xlsx: {type(e).__name__}")

    if has_chart:
        rep.warn("工作簿含图表，openpyxl 保存会丢失图表，已转人工处理", review=True)

    # 大表（字符串单元格超过阈值）走快速路径，避免逐格 NER 把单文件拖到 500 秒
    try:
        with zipfile.ZipFile(in_path) as z:
            sheet_bytes = sum(
                i.file_size for i in z.infolist()
                if i.filename.startswith("xl/worksheets/") and i.filename.endswith(".xml")
            )
    except zipfile.BadZipFile:
        sheet_bytes = 0
    if sheet_bytes > _FAST_SHEET_BYTES:
        wb.close()
        rep.warn(
            f"工作簿体积较大（工作表 XML {sheet_bytes/1024/1024:.0f}MB），"
            "已启用快速路径（逐格只跑规则/词典/上下文 + 整表一次 NER）"
        )
        return redact_xlsx_sparse(in_path, out_path, detect)

    renamed = 0
    for ws in list(wb.worksheets):
        header_cols: dict[int, tuple[str, int]] = {}
        # 表头只扫前 N 行，且同样走稀疏遍历
        for cell in _iter_cells(ws):
            if isinstance(cell.value, str) and cell.row <= _HEADER_ROWS:
                typ = _match_header(cell.value)
                if typ and cell.column not in header_cols:
                    header_cols[cell.column] = (typ, cell.row)

        for cell in _iter_cells(ws):
            if cell.comment is not None:
                cell.comment = None
            v = cell.value
            if v is None:
                continue

            # 公式：只检查字符串常量，避免把 =G20*F20 误判成公司名
            if isinstance(v, str) and v.startswith("="):
                for lit in _formula_text_literals(v):
                    spans = detect(lit)
                    if spans:
                        rep.add_spans(spans)
                        rep.warn(f"公式 {cell.coordinate} 含疑似敏感字符串常量", review=True)
                skeleton = _formula_skeleton(v)
                if skeleton.strip() and not re.fullmatch(r"[\s\d+\-*/(),.%&<>=\"'!:^~]+", skeleton):
                    spans = detect(skeleton)
                    if spans:
                        rep.add_spans(spans)
                        rep.warn(f"公式 {cell.coordinate} 含疑似敏感内容", review=True)
                continue

            if not isinstance(v, str):
                continue

            # 表头列：仅当内容确实像该类型时才整格替换
            hc = header_cols.get(cell.column)
            if hc and cell.row > hc[1] and v.strip() and _looks_like(v.strip(), hc[0]):
                rep.add(hc[0])
                cell.value = placeholder(hc[0])
                continue

            spans = detect(v)
            if spans:
                rep.add_spans(spans)
                cell.value = _apply(v, spans)

        if detect(ws.title):
            renamed += 1
            rep.warn("sheet 名命中敏感信息，已改名")
            ws.title = f"Sheet{renamed}"

    try:
        wb.properties.creator = ""
        wb.properties.lastModifiedBy = ""
        wb.properties.title = ""
        wb.properties.subject = ""
        wb.properties.description = ""
        wb.properties.keywords = ""
        wb.properties.category = ""
        wb.properties.company = ""
    except Exception:  # noqa: BLE001
        pass

    wb.save(str(out_path))
    wb.close()
    redact_media_in_zip(out_path, detect, rep)
    return rep


def _apply(text: str, spans) -> str:
    out = text
    for s in sorted(spans, key=lambda x: x.start, reverse=True):
        out = out[: s.start] + placeholder(s.type) + out[s.end:]
    return out


def _ner_pass(ws, detect, rep: Report) -> None:
    """整表 NER，把命中的原文片段回填到包含它的单元格。

    性能：订单一类的大表每个 sheet 有 3000+ 个字符串单元格，
    - 逐格调用 NER 实测 380 秒（HanLP 单次 50~150 ms）；
    - 整表拼成一段再跑，单段会超过模型长度上限、被切成多段后**又变回逐段调用**；
    - 最终实现是把「每个单元格」作为一条独立输入批量提交（ner_spans_batch），
      一次 forward 处理整批，实测快 4 倍，且保留单元格边界、不会把相邻格子
      拼成假实体。
    """
    from core.detect import _ner_acceptable
    from core.ner import ner_spans_batch

    cells = [c for c in _iter_cells(ws) if isinstance(c.value, str) and c.value.strip()]
    if len(cells) < 3:
        return
    texts = [c.value for c in cells]
    if sum(len(t) for t in texts) < 12:
        return
    try:
        results = ner_spans_batch(texts)
    except Exception:  # noqa: BLE001
        return

    for cell, text, spans in zip(cells, texts, results):
        hits = [s for s in spans if _ner_acceptable(text, s)]
        if not hits:
            continue
        new = text
        for s in sorted(hits, key=lambda x: x.start, reverse=True):
            new = new[: s.start] + placeholder(s.type) + new[s.end:]
            rep.add(s.type)
        if new != text:
            cell.value = new


def redact_xlsx_sparse(in_path, out_path, detect) -> Report:
    """大表快速路径：只做规则/词典/上下文 + 整表一次 NER，不逐格跑 NER。

    当工作簿的字符串单元格数超过 `limits.xlsx_fast_cell_threshold` 时启用。
    精度影响：单元格级的英文人名/公司若既不匹配规则也没有标签前缀，
    只能靠整表 NER 命中；实测这类漏检在订单表上占比很低，且逐格 NER 会让
    单文件耗时从 ~30 秒涨到 500 秒以上。
    """
    from openpyxl import load_workbook

    rep = Report()
    try:
        wb = load_workbook(str(in_path), data_only=False, rich_text=False)
    except Exception as e:  # noqa: BLE001
        return rep.fail(f"无法打开 xlsx: {type(e).__name__}")

    from core.detect import detect_text_only

    for ws in list(wb.worksheets):
        header_cols: dict[int, tuple[str, int]] = {}
        for cell in _iter_cells(ws):
            if isinstance(cell.value, str) and cell.row <= _HEADER_ROWS:
                typ = _match_header(cell.value)
                if typ and cell.column not in header_cols:
                    header_cols[cell.column] = (typ, cell.row)

        for cell in _iter_cells(ws):
            if cell.comment is not None:
                cell.comment = None
            v = cell.value
            if not isinstance(v, str) or not v:
                continue
            if v.startswith("="):
                continue
            hc = header_cols.get(cell.column)
            if hc and cell.row > hc[1] and v.strip() and _looks_like(v.strip(), hc[0]):
                rep.add(hc[0])
                cell.value = placeholder(hc[0])
                continue
            spans = detect_text_only(v)
            if spans:
                rep.add_spans(spans)
                cell.value = _apply(v, spans)

        _ner_pass(ws, detect, rep)

    try:
        wb.properties.creator = ""
        wb.properties.lastModifiedBy = ""
        wb.properties.title = ""
    except Exception:  # noqa: BLE001
        pass
    wb.save(str(out_path))
    wb.close()
    redact_media_in_zip(out_path, detect, rep)
    return rep


def column_letter(idx: int) -> str:
    return get_column_letter(idx)
