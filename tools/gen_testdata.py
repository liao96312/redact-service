"""T0.4 测试数据生成：用 Faker 生成含敏感信息的 docx / xlsx / pdf / png 各 20 份，
并把部分 pdf 转成扫描件；同时输出 testdata/labels.json。

labels.json: { 相对路径: [必须被打码的字符串, ...] }

用法：python tools/gen_testdata.py
"""
from __future__ import annotations

import json
import os
import random
import shutil
from pathlib import Path

import fitz  # PyMuPDF
from docx import Document
from docx.shared import Pt
from faker import Faker
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "testdata"
N = 20
SEED = 20240116

fake_zh = Faker("zh_CN")
fake_en = Faker("en_US")
Faker.seed(SEED)
random.seed(SEED)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]


def _font(size: int):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def gen_record(i: int) -> dict:
    zh_name = fake_zh.name()
    en_name = fake_en.name()
    org_zh = fake_zh.company() + random.choice(["股份有限公司", "有限公司", "有限责任公司"])
    org_en = fake_en.company()
    addr_zh = fake_zh.address().replace("\n", "")
    addr_en = fake_en.address().replace("\n", ", ")
    phone = "1" + random.choice("3456789") + "".join(random.choice("0123456789") for _ in range(9))
    landline = "0" + str(random.randint(10, 999)) + "-" + str(random.randint(1000000, 9999999))
    email = fake_en.email()
    card = _luhn_card(random.choice(["4", "5", "62"]))
    id_card = fake_zh.ssn()
    return {
        "zh_name": zh_name,
        "en_name": en_name,
        "org_zh": org_zh,
        "org_en": org_en,
        "addr_zh": addr_zh,
        "addr_en": addr_en,
        "phone": phone,
        "landline": landline,
        "email": email,
        "card": card,
        "id_card": id_card,
        "labels": {
            "zh_name": zh_name,
            "en_name": en_name,
            "org_zh": org_zh,
            "org_en": org_en,
            "addr_zh": addr_zh,
            "addr_en": addr_en,
            "phone": phone,
            "landline": landline,
            "email": email,
            "card": card,
            "id_card": id_card,
        },
    }


def _luhn_card(prefix: str) -> str:
    body = prefix + "".join(random.choice("0123456789") for _ in range(15 - len(prefix)))
    for d in range(10):
        cand = body + str(d)
        if _luhn_ok(cand):
            return cand
    return body + "0"


def _luhn_ok(num: str) -> bool:
    s = 0
    for i, ch in enumerate(reversed(num)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        s += d
    return s % 10 == 0


# ----------------------------------------------------------------- docx
def make_docx(rec: dict, path: Path) -> None:
    doc = Document()
    p = doc.add_paragraph()
    r1 = p.add_run("客户：")
    r2 = p.add_run(rec["zh_name"])          # 姓名被单独一个 run（测试拼接）
    p2 = doc.add_paragraph()
    p2.add_run("姓名：")
    p2.add_run(rec["zh_name"])
    doc.add_paragraph(f"公司名称：{rec['org_zh']}")
    doc.add_paragraph(f"注册地址：{rec['addr_zh']}")
    doc.add_paragraph(f"联系电话：{rec['phone']}")
    doc.add_paragraph(f"座机：{rec['landline']}")
    doc.add_paragraph(f"银行卡号：{rec['card']}")
    doc.add_paragraph(f"身份证号：{rec['id_card']}")
    doc.add_paragraph(f"邮箱：{rec['email']}")
    doc.add_paragraph(f"Customer Name: {rec['en_name']}")
    doc.add_paragraph(f"Company: {rec['org_en']}")
    doc.add_paragraph(f"Address: {rec['addr_en']}")
    doc.save(str(path))


# ----------------------------------------------------------------- xlsx
def make_xlsx(rec: dict, path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = f"客户-{rec['zh_name']}"  # sheet 名含敏感信息，测试改名
    ws.append(["姓名", "电话", "银行卡", "公司", "地址", "邮箱"])
    ws.append([rec["zh_name"], rec["phone"], rec["card"], rec["org_zh"], rec["addr_zh"], rec["email"]])
    ws.append([rec["en_name"], rec["landline"], "", rec["org_en"], rec["addr_en"], ""])
    ws2 = wb.create_sheet("说明")
    ws2["A1"] = "本文件为测试数据"
    ws2["A2"] = f"负责人：{rec['zh_name']}"
    wb.save(str(path))


# ----------------------------------------------------------------- pdf
def make_pdf(rec: dict, path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    tw = fitz.TextWriter(page.rect)
    font = _pdf_font()
    lines = [
        f"客户资料",
        f"户名：{rec['zh_name']}",
        f"公司名称：{rec['org_zh']}",
        f"注册地址：{rec['addr_zh']}",
        f"联系电话：{rec['phone']}",
        f"银行卡号：{rec['card']}",
        f"身份证号：{rec['id_card']}",
        f"邮箱：{rec['email']}",
        f"Customer: {rec['en_name']}",
        f"Company: {rec['org_en']}",
    ]
    y = 60
    for ln in lines:
        tw.append((50, y), ln, font=font, fontsize=12)
        y += 24
    tw.write_text(page)
    doc.save(str(path))
    doc.close()


def _pdf_font():
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return fitz.Font(fontfile=p)
            except Exception:
                continue
    return fitz.Font("china-s")


def make_scanned(src_pdf: Path, dst_pdf: Path) -> None:
    src = fitz.open(str(src_pdf))
    out = fitz.open()
    for page in src:
        pix = page.get_pixmap(dpi=150)
        img = pix.tobytes("png")
        newpage = out.new_page(width=page.rect.width, height=page.rect.height)
        newpage.insert_image(newpage.rect, stream=img)
    out.save(str(dst_pdf))
    out.close()
    src.close()


# ----------------------------------------------------------------- png
def make_png(rec: dict, path: Path) -> None:
    img = Image.new("RGB", (900, 520), "white")
    d = ImageDraw.Draw(img)
    f = _font(22)
    lines = [
        "客户资料",
        f"户名：{rec['zh_name']}",
        f"公司名称：{rec['org_zh']}",
        f"注册地址：{rec['addr_zh']}",
        f"联系电话：{rec['phone']}",
        f"银行卡号：{rec['card']}",
        f"身份证号：{rec['id_card']}",
        f"邮箱：{rec['email']}",
        f"Customer: {rec['en_name']}",
    ]
    y = 20
    for ln in lines:
        d.text((20, y), ln, fill="black", font=f)
        y += 50
    img.save(str(path))


def main() -> None:
    for sub in ("docx", "xlsx", "pdf", "png", "scanned"):
        (OUT / sub).mkdir(parents=True, exist_ok=True)
    labels: dict[str, list[str]] = {}
    typed: dict[str, dict[str, list[str]]] = {}
    recs = [gen_record(i) for i in range(N)]
    for i, rec in enumerate(recs, 1):
        vals = list(rec["labels"].values())
        by_type = {
            "PERSON": [rec["zh_name"], rec["en_name"]],
            "ORG": [rec["org_zh"], rec["org_en"]],
            "ADDRESS": [rec["addr_zh"], rec["addr_en"]],
            "PHONE": [rec["phone"], rec["landline"]],
            "BANK_CARD": [rec["card"]],
            "ID_CARD": [rec["id_card"]],
            "EMAIL": [rec["email"]],
        }
        dp = OUT / "docx" / f"{i:02d}.docx"
        make_docx(rec, dp)
        labels[f"docx/{dp.name}"] = vals
        typed[f"docx/{dp.name}"] = by_type

        xp = OUT / "xlsx" / f"{i:02d}.xlsx"
        make_xlsx(rec, xp)
        labels[f"xlsx/{xp.name}"] = vals
        typed[f"xlsx/{xp.name}"] = by_type

        pp = OUT / "pdf" / f"{i:02d}.pdf"
        make_pdf(rec, pp)
        labels[f"pdf/{pp.name}"] = vals
        typed[f"pdf/{pp.name}"] = by_type

        sp = OUT / "scanned" / f"{i:02d}.pdf"
        make_scanned(pp, sp)
        labels[f"scanned/{sp.name}"] = vals
        typed[f"scanned/{sp.name}"] = by_type

        gp = OUT / "png" / f"{i:02d}.png"
        make_png(rec, gp)
        labels[f"png/{gp.name}"] = vals
        typed[f"png/{gp.name}"] = by_type

    (OUT / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "labels_typed.json").write_text(
        json.dumps(typed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    total = sum(1 for _ in OUT.rglob("*") if _.is_file())
    print(f"generated {len(labels)} labeled files, total files={total}, labels.json + labels_typed.json written")


if __name__ == "__main__":
    main()
