"""T4.1 评测：对 testdata 全量跑一遍，按类型输出召回率与误杀率，写 reports/eval.md。

- 召回：labels_typed.json 中的字符串是否仍残留在输出文本中（残留=漏检）
- 误杀率：在无敏感信息的干净语料上统计误报
- verify 通过率：dispatch 状态为 done 的比例
- 漏检样例（脱敏描述，不含明文）写 reports/misses.md

用法：python tools/evaluate.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from handlers.dispatch import dispatch_redact, sniff  # noqa: E402
from handlers.verify import extract_text  # noqa: E402

TESTDATA = ROOT / "testdata"
REPORTS = ROOT / "reports"

CLEAN_CORPUS = [
    "本季度销售报告显示，华东区营收同比增长百分之十二，达成既定目标。",
    "The meeting is scheduled for next Monday at 3 PM in room 201.",
    "订单编号 20240115001，数量 5 件，单价 19.9 元，合计 99.5 元。",
    "会议纪要：项目按计划推进，下阶段重点优化性能与稳定性。",
    "Please review the attached budget spreadsheet before Friday afternoon.",
    "产品编号：AB-1002；入库时间：2024年3月。",
]


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def eval_file(rel: str, by_type: dict) -> dict:
    src = TESTDATA / rel
    kind = sniff(src)
    result = {"status": "", "type_total": {}, "type_miss": {}, "misses": []}
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / ("out_" + src.name)
        rep = dispatch_redact(src, out, "")
        result["status"] = rep.status
        if not out.exists() or rep.status == "failed":
            # 全部计为漏检
            for t, vals in by_type.items():
                result["type_total"][t] = result["type_total"].get(t, 0) + len(vals)
                result["type_miss"][t] = result["type_miss"].get(t, 0) + len(vals)
                for v in vals:
                    result["misses"].append((t, _describe(v)))
            return result
        text = norm(extract_text(out, kind))
        for t, vals in by_type.items():
            for v in vals:
                result["type_total"][t] = result["type_total"].get(t, 0) + 1
                if v and norm(v) in text:
                    result["type_miss"][t] = result["type_miss"].get(t, 0) + 1
                    result["misses"].append((t, _describe(v)))
    return result


def _describe(v: str) -> str:
    """脱敏描述：只给类型/长度/字符类别，绝不输出明文。"""
    kinds = []
    if any(c.isdigit() for c in v):
        kinds.append("含数字")
    if any("\u4e00" <= c <= "\u9fa5" for c in v):
        kinds.append("含汉字")
    if any(c.isalpha() and c.isascii() for c in v):
        kinds.append("含拉丁字母")
    return f"len={len(v)}({','.join(kinds) or '其他'})"


def clean_false_positives() -> tuple[int, int]:
    from core.detect import detect

    fp = 0
    chars = 0
    for s in CLEAN_CORPUS:
        chars += len(norm(s))
        fp += len(detect(s))
    return fp, chars


def main() -> None:
    typed = json.loads((TESTDATA / "labels_typed.json").read_text(encoding="utf-8"))
    REPORTS.mkdir(exist_ok=True)

    agg_total: dict[str, int] = {}
    agg_miss: dict[str, int] = {}
    status_count: dict[str, int] = {}
    all_misses = []
    by_kind: dict[str, dict] = {}

    for rel, bt in typed.items():
        r = eval_file(rel, bt)
        status_count[r["status"]] = status_count.get(r["status"], 0) + 1
        k = rel.split("/")[0]
        bk = by_kind.setdefault(k, {"total": {}, "miss": {}, "files": 0})
        bk["files"] += 1
        for t, n in r["type_total"].items():
            agg_total[t] = agg_total.get(t, 0) + n
            bk["total"][t] = bk["total"].get(t, 0) + n
        for t, n in r["type_miss"].items():
            agg_miss[t] = agg_miss.get(t, 0) + n
            bk["miss"][t] = bk["miss"].get(t, 0) + n
        for t, d in r["misses"]:
            all_misses.append(f"- {rel}  {t}  {d}")
        print(f"  {rel:24} {r['status']:13} miss={sum(r['type_miss'].values())}")

    types = ["PHONE", "BANK_CARD", "ID_CARD", "PERSON", "ORG", "ADDRESS", "EMAIL"]
    lines = ["# 评测报告 (eval.md)", "", "## 总体召回率", "", "| 类型 | 命中 | 漏检 | 召回率 | 目标 | 是否达标 |", "|---|---|---|---|---|---|"]
    targets = {"PHONE": 0.99, "BANK_CARD": 0.99, "ID_CARD": 0.99, "PERSON": 0.90, "ORG": 0.90, "ADDRESS": 0.90, "EMAIL": 0.90}
    for t in types:
        tot = agg_total.get(t, 0)
        miss = agg_miss.get(t, 0)
        rec = (tot - miss) / tot if tot else 1.0
        tgt = targets[t]
        lines.append(f"| {t} | {tot - miss} | {miss} | {rec:.2%} | {tgt:.0%} | {'✅' if rec >= tgt else '❌'} |")

    total_status = sum(status_count.values())
    done = status_count.get("done", 0)
    pass_rate = done / total_status if total_status else 0.0
    lines += ["", "## verify 通过率", "", f"- done: {done}/{total_status} = {pass_rate:.2%}（目标 ≥95%）"]
    lines += ["", "## 状态分布", ""]
    for k, v in sorted(status_count.items()):
        lines.append(f"- {k}: {v}")

    fp, chars = clean_false_positives()
    lines += ["", "## 误杀率（干净语料）", "",
              f"- 干净样本 {len(CLEAN_CORPUS)} 段，共 {chars} 字",
              f"- 误报 span 数: {fp}",
              f"- 每万字误报: {fp / (chars / 10000):.2f}"]

    lines += ["", "## 分文件类型", "", "| 类型 | 文件数 | 各实体召回率 |", "|---|---|---|"]
    for k, bk in by_kind.items():
        pcts = []
        for t in types:
            tot = bk["total"].get(t, 0)
            if tot:
                pcts.append(f"{t}:{(tot - bk['miss'].get(t, 0)) / tot:.0%}")
        lines.append(f"| {k} | {bk['files']} | {', '.join(pcts) or '-'} |")

    (REPORTS / "eval.md").write_text("\n".join(lines), encoding="utf-8")

    if all_misses:
        (REPORTS / "misses.md").write_text("# 漏检样例（脱敏描述）\n\n" + "\n".join(all_misses) + "\n", encoding="utf-8")
    else:
        (REPORTS / "misses.md").write_text("# 漏检样例\n\n无\n", encoding="utf-8")

    print("\n=== 汇总 ===")
    for t in types:
        tot = agg_total.get(t, 0)
        miss = agg_miss.get(t, 0)
        print(f"{t:10} recall={((tot-miss)/tot if tot else 1):.2%} ({tot-miss}/{tot})")
    print(f"verify 通过率={pass_rate:.2%}  干净语料误报={fp}")
    print("报告已写入 reports/eval.md, reports/misses.md")


if __name__ == "__main__":
    main()
