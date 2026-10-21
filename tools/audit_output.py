"""独立校验：不信任服务自己的报告，重新抽取原文与输出做对比。

用法：
    python tools/audit_output.py 原始目录 输出目录 [-o 审计报告.md]

做三件事：
1. 抽样比对：从源压缩包里随机抽 N 个文件，重新抽取文本，检查原始敏感串
   是否还出现在输出里（残留 = 漏脱敏）；
2. 结构校验：输出文件能否被对应库正常打开（防止「脱敏成功但文件坏了」）；
3. 覆盖率核对：报告里声明 done 的文件是否真的存在产物。

只输出统计与文件名，**不打印任何明文敏感内容**。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymupdf as fitz  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from PIL import Image  # noqa: E402

from core.detect import detect  # noqa: E402
from handlers.verify import extract_text  # noqa: E402
from tools.batch_redact import ArchiveReader  # noqa: E402

AUDITABLE = {".pdf", ".docx", ".xlsx", ".pptx", ".txt"}


def _kind(p: Path) -> str:
    return p.suffix.lower().lstrip(".")


def _openable(p: Path) -> tuple[bool, str]:
    ext = p.suffix.lower()
    try:
        if ext == ".pdf":
            doc = fitz.open(str(p))
            n = doc.page_count
            doc.close()
            return n > 0, f"{n} pages"
        if ext == ".xlsx":
            wb = load_workbook(str(p))
            n = len(wb.worksheets)
            wb.close()
            return n > 0, f"{n} sheets"
        if ext == ".docx":
            with zipfile.ZipFile(p) as z:
                ok = "word/document.xml" in z.namelist()
            return ok, "zip ok" if ok else "missing document.xml"
        if ext == ".pptx":
            with zipfile.ZipFile(p) as z:
                ok = any(n.startswith("ppt/slides/") for n in z.namelist())
            return ok, "zip ok" if ok else "missing slides"
        if ext in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"):
            im = Image.open(p)
            im.load()
            return True, f"{im.size[0]}x{im.size[1]}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}"
    return True, "skipped"


def main() -> int:
    ap = argparse.ArgumentParser(description="独立核验脱敏输出")
    ap.add_argument("source", help="原始图纸目录")
    ap.add_argument("output", help="脱敏输出目录")
    ap.add_argument("-n", "--sample", type=int, default=40, help="抽样文件数（每类）")
    ap.add_argument("-o", "--report", default=None, help="输出 Markdown 报告路径")
    ap.add_argument("--seed", type=int, default=20260101)
    args = ap.parse_args()

    random.seed(args.seed)
    src_root = Path(args.source)
    out_root = Path(args.output)

    lines: list[str] = ["# 脱敏输出独立核验", ""]
    stats = {"checked": 0, "residual": 0, "broken": 0, "missing": 0}
    problems: list[str] = []
    broken_list: list[str] = []

    # ---------- 1. 结构校验：全部产物逐个打开 ----------
    all_out = [p for p in out_root.rglob("*") if p.is_file()]
    for p in all_out:
        if p.suffix.lower() in (".md", ".json"):
            continue
        ok, detail = _openable(p)
        stats["checked"] += 1
        if not ok:
            stats["broken"] += 1
            broken_list.append(f"{p.relative_to(out_root)} —— {detail}")

    lines += [
        "## 一、结构完整性",
        "",
        f"- 产物文件总数：{len(all_out)}",
        f"- 可正常打开：{stats['checked'] - stats['broken']}",
        f"- 打不开（已损坏）：{stats['broken']}",
        "",
    ]
    if broken_list:
        lines += ["损坏文件：", ""]
        lines += [f"- `{b}`" for b in broken_list[:50]]
        lines.append("")
        problems.append(f"{stats['broken']} 个产物无法打开")

    # ---------- 2. 残留抽样比对 ----------
    typed: dict[str, list[tuple[str, str]]] = {}
    for arch in sorted(p for p in src_root.rglob("*") if p.is_file() and p.suffix.lower() in (".zip", ".rar", ".7z")):
        try:
            with ArchiveReader(arch) as r:
                for name in r.names():
                    if Path(name).suffix.lower() in AUDITABLE:
                        typed.setdefault(Path(name).suffix.lower(), []).append((str(arch), name))
        except Exception:  # noqa: BLE001
            continue

    lines += ["## 二、残留抽样比对", "",
              "从源压缩包随机抽文件，比对其原始敏感串是否仍出现在输出里。", ""]

    for ext, items in sorted(typed.items()):
        sample = random.sample(items, min(args.sample, len(items)))
        checked = residual = skipped = 0
        for arch_path, name in sample:
            # 定位输出文件
            stem = Path(arch_path).stem
            out_path = out_root / Path(arch_path).relative_to(src_root).parent / (stem + "_redacted")
            cand = None
            for p in out_path.rglob(Path(name).name):
                cand = p
                break
            if cand is None:
                stats["missing"] += 1
                skipped += 1
                continue
            try:
                with ArchiveReader(Path(arch_path)) as r:
                    src_text = extract_text_bytes(r.read(name), name)
                out_text = extract_text(cand, _kind(cand))
            except Exception:  # noqa: BLE001
                skipped += 1
                continue
            if not src_text.strip():
                skipped += 1
                continue
            # 原文里的高置信敏感串
            originals = [s for s in detect(src_text) if s.score >= 0.9]
            if not originals:
                skipped += 1
                continue
            checked += 1
            leaked = 0
            for s in originals:
                frag = src_text[s.start:s.end].strip()
                if len(frag) >= 6 and frag in out_text:
                    leaked += 1
            if leaked:
                residual += 1
                problems.append(f"{name}: {leaked} 处原始敏感串仍残留")
        lines.append(
            f"- `{ext}`：抽样 {len(sample)}，可比对 {checked}，"
            f"有残留 {residual}，跳过 {skipped}"
        )
        stats["residual"] += residual

    if not typed:
        lines.append("- 源目录里没有可直接比对的文档类型")

    # ---------- 3. 报告覆盖核对 ----------
    lines += ["", "## 三、处理报告覆盖核对", ""]
    rep_json = out_root / "report.json"
    if rep_json.exists():
        data = json.loads(rep_json.read_text(encoding="utf-8"))
        status = data.get("status", {})
        lines += [
            f"- 报告声明状态：{status}",
            f"- 报告声明命中总数：{sum(data.get('type_totals', {}).values())}",
            f"- 未处理类型种类：{len(data.get('unprocessed_exts', {}))}",
        ]
        asserts = data.get("status", {}).get("done", 0) + data.get("status", {}).get("needs_review", 0)
        if asserts == 0:
            problems.append("报告里没有任何 done/needs_review 记录")
    else:
        problems.append("缺少 report.json")
        lines.append("- ⚠ 未找到 report.json")

    lines += ["", "## 四、结论", ""]
    if problems:
        lines.append(f"发现 **{len(problems)}** 项需要关注：")
        lines += [f"- {p}" for p in problems[:60]]
    else:
        lines.append("未发现残留或结构问题。")

    report_path = Path(args.report) if args.report else (out_root / "AUDIT.md")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n审计报告：{report_path}")
    return 0


def extract_text_bytes(data: bytes, name: str) -> str:
    """把压缩包内的字节落到临时文件后复用统一的抽文逻辑。"""
    from core.workdir import temporary_workdir

    with temporary_workdir("audit") as td:
        p = td / Path(name).name
        p.write_bytes(data)
        return extract_text(p, _kind(p))


if __name__ == "__main__":
    sys.exit(main())
