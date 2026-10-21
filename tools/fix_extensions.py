"""按真实文件头修正输出目录里的扩展名。

背景：`.xls` / `.et` 会被转成 xlsx 内容，若沿用原扩展名，Excel 打开会报
「文件格式与扩展名不符」。批处理里已经按产物类型改扩展名；这个脚本用于
**事后修复**已经产出的目录（例如批处理跑的是旧版本）。

用法：
    python tools/fix_extensions.py D:\\redacted-drawings [--apply]
不带 --apply 时只打印计划，不改动文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from handlers.dispatch import kind_of  # noqa: E402

# 内容类型 -> 期望扩展名
WANT = {
    "docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx", "pdf": ".pdf",
    "png": ".png", "jpg": ".jpg", "bmp": ".bmp", "tif": ".tif",
}
# 只处理这些扩展名（避免误改 .dwg/.bak 等本就不处理的文件）
CANDIDATE_EXTS = {".xls", ".et", ".doc", ".tmp", ".bin"}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1]).resolve()
    apply = "--apply" in sys.argv
    if not root.is_dir():
        print(f"目录不存在: {root}")
        return 2

    planned = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in CANDIDATE_EXTS:
            continue
        try:
            kind = kind_of(p)
        except Exception:  # noqa: BLE001
            continue
        want = WANT.get(kind)
        if not want or p.suffix.lower() == want:
            continue
        target = p.with_suffix(want)
        if target.exists():
            print(f"跳过（目标已存在）: {p.relative_to(root)}")
            continue
        rel = p.relative_to(root)
        print(f"{'改名' if apply else '计划'}: {rel} -> {target.name}")
        planned += 1
        if apply:
            try:
                p.rename(target)
            except OSError as e:
                print(f"  失败: {e}")

    print(f"\n共 {planned} 个文件{'已改名' if apply else '待改名（加 --apply 执行）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
