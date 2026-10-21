"""批量脱敏：递归处理目录 / 压缩包（含嵌套 zip/rar/7z）里的文档与图片。

用法：
    # 处理单个目录
    python tools/batch_redact.py "C:\\Users\\x\\Desktop\\图纸" -o "D:\\redacted"

    # 只处理散单、限制文件数、跳过 OCR 慢路径
    python tools/batch_redact.py 源目录 -o 输出 --only 散单 --max-files 50

设计要点：
- **只读源文件**：源目录一个字节都不写，所有产物写到 -o 指定的目录，
  且保持包内相对路径，方便原样替换；
- 压缩包流式读取：3.2GB 的工程包不会整包解压到磁盘，只把「要处理的文件」
  逐个抽到工作目录，处理完立即删除；
- 嵌套压缩包（zip 里套 zip/rar）默认展开 2 层；
- 无法处理的类型（dwg/bak/数据库/日志/视频）如实记入报告，不静默跳过；
- 报告只写「类型 + 数量 + 状态」，**不写任何明文敏感内容**。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 批量离线处理放宽单文件上限（接口默认 50MB；图纸包里的 3D 方案 pptx 常 60~100MB）。
# 必须在**任何** cfg() 调用与 handlers.dispatch 导入之前设置：
# core.config.load_config 带 lru_cache，先读一次就定型了。
os.environ.setdefault("REDACT_MAX_FILE_MB", "300")

from core.config import cfg  # noqa: E402
from core.workdir import temporary_workdir  # noqa: E402
from handlers.dispatch import kind_of, shutdown_pool  # noqa: E402

# 可处理类型（不含点）-> 输出扩展名
PROCESSABLE = {
    "pdf", "docx", "xlsx", "pptx", "doc", "xls", "et",
    "png", "jpg", "jpeg", "bmp", "tif", "tiff", "gif", "webp",
}
ARCHIVES = {".zip", ".rar", ".7z"}
# 明确不处理、但要在报告里点名的类型（含敏感文本但无解析器）
UNSUPPORTED_KNOWN = {
    ".dwg": "CAD 图纸（二进制，标题栏常含客户名/项目名，本项目无 DWG 解析器）",
    ".bak": "CAD 备份文件（同 .dwg）",
    ".dwl": "CAD 锁文件", ".dwl2": "CAD 锁文件",
    ".db": "缩略图/本地数据库", ".swj": "SolidWorks 中间文件",
    ".project": "CAD 项目索引", ".log": "打印/操作日志（可能含路径）",
    ".mp4": "视频", ".lnk": "快捷方式", ".err": "错误日志",
    ".lck": "锁文件", ".cfg": "配置文件",
}
TEXT_EXTS = {".txt", ".csv", ".json", ".md", ".ini"}

SEVENZIP_CANDIDATES = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    "7z",
    "/usr/bin/7z",
]


def sevenzip() -> str | None:
    raw = os.environ.get("REDACT_7Z")
    if raw and Path(raw).exists():
        return raw
    for cand in SEVENZIP_CANDIDATES:
        if Path(cand).exists():
            return cand
        found = shutil.which(cand)
        if found:
            return found
    return None


# ---------------------------------------------------------------- 数据模型
@dataclass
class ItemResult:
    archive: str
    inner: str
    kind: str
    status: str
    counts: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    message: str = ""
    out: str = ""
    seconds: float = 0.0


class Stats:
    """累计统计 + 增量进度日志。

    为什么需要日志：一整套图纸要跑 1~3 小时，中途被杀（超时/重启/断电）时，
    只在结束时写 report.json 的话所有进度都会丢。这里每处理完一个文件就追加
    一行 JSONL，配合 `--resume` 可跳过已完成项，最终报告也能从日志重建。
    """

    def __init__(self, journal: Path | None = None) -> None:
        self.items: list[ItemResult] = []
        self.skipped: Counter = Counter()
        self.unsupported: dict[str, Counter] = defaultdict(Counter)
        self.errors: list[str] = []
        self.type_totals: Counter = Counter()
        self.status: Counter = Counter()
        self.per_archive: dict[str, Counter] = defaultdict(Counter)
        self.journal = journal
        self._fh = None
        if journal is not None:
            journal.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(journal, "a", encoding="utf-8", buffering=1)

    def add(self, r: ItemResult) -> None:
        self.items.append(r)
        self.status[r.status] += 1
        for k, v in r.counts.items():
            self.type_totals[k] += v
        self.per_archive[r.archive][r.status] += 1
        self._log(r)

    def _log(self, r: ItemResult) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(json.dumps({
                "archive": r.archive,
                "file": r.inner,
                "kind": r.kind,
                "status": r.status,
                "counts": r.counts,
                "warnings": r.warnings[:6],
                "message": r.message,
                "seconds": r.seconds,
            }, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def replay(self, journal: Path) -> set[tuple[str, str]]:
        """从既有日志恢复统计，返回已完成的 (archive, file) 集合。

        注意：**失败项不恢复**。它们会在本轮被重试，若先计入统计再重试就会重复
        计数（实测 4 个超限 pptx 让总数从 846 变成 850，且 failed 永远归不了零）。
        同一条目在日志里出现多次时，以最后一次为准。
        """
        done: set[tuple[str, str]] = set()
        if not journal.exists():
            return done

        latest: dict[tuple[str, str], dict] = {}
        order: list[tuple[str, str]] = []
        for line in journal.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (d.get("archive", ""), d.get("file", ""))
            if key not in latest:
                order.append(key)
            latest[key] = d

        for key in order:
            d = latest[key]
            status = d.get("status", "")
            if status == "failed":
                continue                      # 本轮重试
            r = ItemResult(
                archive=key[0],
                inner=key[1],
                kind=d.get("kind", ""),
                status=status,
                counts=d.get("counts") or {},
                warnings=d.get("warnings") or [],
                message=d.get("message", ""),
                seconds=d.get("seconds", 0.0),
            )
            self.add(r)
            if r.status in ("done", "needs_review", "skipped"):
                done.add((r.archive, r.inner))
        return done

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def note_skip(self, name: str) -> None:
        self.skipped[Path(name).suffix.lower() or "(无扩展名)"] += 1


# ---------------------------------------------------------------- 归档读取
class ArchiveReader:
    """统一 zip / rar / 7z 的读取接口（逐个条目流式读取）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.kind = "zip" if zipfile.is_zipfile(path) else "7z"
        self._sevenz = sevenzip()
        if self.kind == "zip":
            self._zf = zipfile.ZipFile(path)
            self._names = [i.filename for i in self._zf.infolist() if not i.is_dir()]
        else:
            if not self._sevenz:
                raise RuntimeError("需要 7-Zip 才能读取 rar/7z")
            self._names = self._list_7z()

    def _list_7z(self) -> list[str]:
        cmd = [self._sevenz, "l", "-slt", "-ba", "-sccUTF-8", str(self.path)]
        proc = subprocess.run(cmd, capture_output=True)
        text = proc.stdout.decode("utf-8", "replace")
        names = []
        for block in text.split("\n\n"):
            path_line = None
            is_dir = False
            for line in block.splitlines():
                if line.startswith("Path = "):
                    path_line = line[7:]
                elif line.startswith("Folder = ") and line[9:].strip() == "+":
                    is_dir = True
            if path_line and not is_dir:
                names.append(path_line)
        return names

    def names(self) -> list[str]:
        return list(self._names)

    def read(self, name: str) -> bytes:
        if self.kind == "zip":
            return self._zf.read(name)
        if not self._sevenz:
            raise RuntimeError("需要 7-Zip")
        with temporary_workdir("x7") as td:
            cmd = [self._sevenz, "e", "-y", "-sccUTF-8", f"-o{td}", str(self.path), name]
            proc = subprocess.run(cmd, capture_output=True)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:300])
            files = [p for p in Path(td).rglob("*") if p.is_file()]
            if not files:
                raise RuntimeError("7z 未产出文件")
            return files[0].read_bytes()

    def close(self) -> None:
        if self.kind == "zip":
            self._zf.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# 旧格式转换后的真实扩展名：.xls 会被转成 xlsx 内容，若仍叫 .xls，
# Excel 打开会报「文件格式与扩展名不符」。
_OUT_EXT = {
    "docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx", "pdf": ".pdf",
    "png": ".png", "jpg": ".jpg", "bmp": ".bmp", "tif": ".tif",
}


def _final_out_path(out_path: Path, kind: str) -> Path:
    """按产物真实类型修正扩展名（.xls/.et → .xlsx，.doc → .docx）。"""
    want = _OUT_EXT.get(kind)
    if not want:
        return out_path
    if out_path.suffix.lower() == want:
        return out_path
    return out_path.with_suffix(want)


# ---------------------------------------------------------------- 处理
def _strip_redundant_root(names: list[str], stem: str) -> str:
    """返回需要从包内路径里剥掉的冗余顶层目录名（没有则空串）。

    图纸包的典型结构是 `<与压缩包同名的文件夹>/项目资料/...`，输出时又会
    写成 `<同名>_redacted/`，于是同一个名字出现两次。在 Windows 上这直接把
    路径推到 MAX_PATH(260) 之外——实测一条 270 字符的路径导致 7z 解包
    FileNotFoundError，整个 247MB 的嵌套 rar 都没处理成。
    """
    tops = {n.split("/", 1)[0] for n in names if "/" in n}
    if len(tops) != 1:
        return ""
    top = tops.pop()
    if not top:
        return ""
    norm = lambda s: re.sub(r"[\s_\-]+", "", s).lower()  # noqa: E731
    return top if norm(top) == norm(stem) else ""


def _shorten_out_dir(out_dir: Path) -> Path:
    """路径过长时把最后一级目录名压短（保留可读前缀 + 短哈希）。"""
    if len(str(out_dir)) <= 180:
        return out_dir
    import hashlib

    tag = hashlib.sha1(str(out_dir).encode("utf-8")).hexdigest()[:8]
    short = f"{out_dir.name[:24]}_{tag}"
    return out_dir.parent / short


def redact_one(data: bytes, filename: str, out_path: Path, timeout_note: list) -> ItemResult:
    from handlers.dispatch import dispatch_redact

    t0 = time.time()
    # 压缩包内的条目名自带目录，落盘只用文件名（避免 < 目录不存在 > 的写入失败）
    safe = Path(filename).name or "file"
    with temporary_workdir("file") as td:
        src = td / safe
        try:
            src.write_bytes(data)
        except OSError as e:
            return ItemResult("", filename, "", "failed", message=f"写入工作目录失败: {e}")
        kind = kind_of(src)
        out = td / ("redacted_" + safe)
        rep = dispatch_redact(src, out, None)
        if rep.status in ("done", "needs_review") and out.exists():
            final_kind = kind_of(out)
            target = _final_out_path(out_path, final_kind)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(out, target)
            out_path = target
        return ItemResult(
            archive="",
            inner=filename,
            kind=kind,
            status=rep.status,
            counts=dict(rep.counts),
            warnings=list(rep.warnings)[:12],
            message=rep.message,
            out=str(out_path),
            seconds=round(time.time() - t0, 2),
        )


def handle_archive(
    reader: ArchiveReader,
    archive_label: str,
    out_dir: Path,
    stats: Stats,
    depth: int,
    max_depth: int,
    max_files: int,
    processed: list,
    only_exts: set[str] | None,
    done_before: set[tuple[str, str]] | None = None,
) -> None:
    names = reader.names()
    # 去掉与压缩包同名的冗余顶层目录（Windows 长路径保护）
    strip_root = _strip_redundant_root(names, Path(archive_label).name)
    if strip_root:
        out_dir = _shorten_out_dir(out_dir)

    def target_rel(name: str) -> str:
        if strip_root and name.startswith(strip_root + "/"):
            return name[len(strip_root) + 1:]
        return name

    for name in names:
        if max_files and len(processed) >= max_files:
            return
        # 续跑：已成功处理过的条目直接跳过（连解压都省掉）
        if done_before and (archive_label, name) in done_before:
            continue
        ext = Path(name).suffix.lower()
        if only_exts and ext not in only_exts:
            continue

        # 单个条目失败绝不允许影响整包：
        # 实测一条 270 字符的输出路径（超过 Windows MAX_PATH）让
        # shutil.copyfile 抛 FileNotFoundError，异常一路冒到 main，
        # 导致该压缩包里 APARTMENT.rar 之后的 82 个文件全部被跳过。
        try:
            _handle_entry(
                reader, name, ext, archive_label, out_dir, target_rel, stats,
                depth, max_depth, max_files, processed, done_before,
            )
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"{archive_label}!{name}: 处理失败 {type(e).__name__}: {str(e)[:160]}")
            print(f"  [error       ] {Path(name).name[:60]}  {type(e).__name__}", flush=True)


def _handle_entry(
    reader: ArchiveReader,
    name: str,
    ext: str,
    archive_label: str,
    out_dir: Path,
    target_rel,
    stats: Stats,
    depth: int,
    max_depth: int,
    max_files: int,
    processed: list,
    done_before: set[tuple[str, str]] | None,
) -> None:
    # 嵌套压缩包
    if ext in ARCHIVES:
        if depth >= max_depth:
            stats.note_skip(name)
            return
        try:
            data = reader.read(name)
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"{archive_label}!{name}: 读取失败 {type(e).__name__}")
            return
        nested_out = _shorten_out_dir(out_dir / (Path(name).stem + "_redacted"))
        try:
            with temporary_workdir("nested") as td:
                p = td / Path(name).name
                p.write_bytes(data)
                with ArchiveReader(p) as sub:
                    handle_archive(
                        sub, f"{archive_label}!{name}", nested_out, stats, depth + 1,
                        max_depth, max_files, processed, None, done_before,
                    )
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"{archive_label}!{name}: 嵌套包无法打开 {type(e).__name__}: {str(e)[:120]}")
        return

    if ext in TEXT_EXTS:
        try:
            data = reader.read(name)
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"{archive_label}!{name}: 读取失败 {type(e).__name__}")
            return
        r = _redact_text(data, name, out_dir / target_rel(name))
        r.archive = archive_label
        stats.add(r)
        processed.append(name)
        return

    if ext.lstrip(".") not in PROCESSABLE:
        stats.unsupported[archive_label][ext or "(无扩展名)"] += 1
        stats.note_skip(name)
        return

    try:
        data = reader.read(name)
    except Exception as e:  # noqa: BLE001
        stats.errors.append(f"{archive_label}!{name}: 读取失败 {type(e).__name__}")
        return

    r = redact_one(data, name, out_dir / target_rel(name), [])
    r.archive = archive_label
    stats.add(r)
    processed.append(name)
    print(f"  [{r.status:12}] {Path(name).name[:70]}  ({r.seconds}s) {r.counts or ''}", flush=True)


def _redact_text(data: bytes, name: str, out_path: Path) -> ItemResult:
    """纯文本：按行检测替换（保留换行与编码）。"""
    from core.detect import detect

    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return ItemResult("", name, "text", "skipped", message="编码无法识别")

    from core.config import placeholder

    counts: Counter = Counter()
    out_lines = []
    for line in text.splitlines(keepends=True):
        spans = detect(line)
        if not spans:
            out_lines.append(line)
            continue
        for s in spans:
            counts[s.type] += 1
        out = line
        for s in sorted(spans, key=lambda x: x.start, reverse=True):
            out = out[: s.start] + placeholder(s.type) + out[s.end:]
        out_lines.append(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(out_lines), encoding="utf-8")
    status = "done" if counts else "skipped"
    return ItemResult("", name, "text", status, counts=dict(counts), out=str(out_path))


def process_loose_file(src: Path, out_path: Path, stats: Stats, processed: list) -> None:
    ext = src.suffix.lower()
    if ext in ARCHIVES:
        return
    if ext in TEXT_EXTS:
        r = _redact_text(src.read_bytes(), src.name, out_path)
        r.archive = "(散落文件)"
        stats.add(r)
        processed.append(str(src))
        return
    if ext.lstrip(".") not in PROCESSABLE:
        stats.unsupported["(散落文件)"][ext or "(无扩展名)"] += 1
        stats.note_skip(src.name)
        return
    r = redact_one(src.read_bytes(), src.name, out_path, [])
    r.archive = "(散落文件)"
    stats.add(r)
    processed.append(str(src))
    print(f"  [{r.status:12}] {src.name[:70]}  ({r.seconds}s) {r.counts or ''}", flush=True)


# ---------------------------------------------------------------- 报告
def write_report(
    stats: Stats, out_root: Path, elapsed: float, src_root: Path,
    previous_done: set | None = None,
) -> None:
    types = ["PERSON", "ORG", "ADDRESS", "PHONE", "BANK_CARD", "ID_CARD", "EMAIL"]
    lines = [
        "# 图纸敏感信息脱敏报告",
        "",
        f"- 源目录：`{src_root}`",
        f"- 输出目录：`{out_root}`",
        f"- 总耗时：{elapsed/60:.1f} 分钟",
        f"- 处理文件数：{len(stats.items)}",
        "",
        "## 状态分布",
        "",
        "| 状态 | 数量 | 含义 |",
        "|---|---|---|",
    ]
    meanings = {
        "done": "脱敏完成，复核通过",
        "needs_review": "已脱敏但有告警（低置信 OCR / 图表会丢失等），建议抽查",
        "failed": "处理失败（损坏/加密/超限）",
        "skipped": "无需处理（缩略图缓存、无正文）",
    }
    for k, v in stats.status.most_common():
        lines.append(f"| {k} | {v} | {meanings.get(k, '')} |")

    lines += ["", "## 命中统计（按类型）", "", "| 类型 | 命中次数 |", "|---|---|"]
    for t in types:
        if stats.type_totals.get(t):
            lines.append(f"| {t} | {stats.type_totals[t]} |")
    lines.append(f"| **合计** | **{sum(stats.type_totals.values())}** |")

    lines += ["", "## 按压缩包", "", "| 压缩包 | done | needs_review | failed | skipped |", "|---|---|---|---|---|"]
    for arch, c in sorted(stats.per_archive.items()):
        lines.append(
            f"| {arch} | {c.get('done',0)} | {c.get('needs_review',0)} | "
            f"{c.get('failed',0)} | {c.get('skipped',0)} |"
        )

    if stats.unsupported:
        lines += ["", "## 未处理类型（需人工确认）", "",
                  "以下类型本服务没有解析器，**没有做脱敏**，如含客户信息需另行处理：", "",
                  "| 位置 | 扩展名 | 数量 | 说明 |", "|---|---|---|---|"]
        for arch, c in sorted(stats.unsupported.items()):
            for ext, n in c.most_common():
                note = UNSUPPORTED_KNOWN.get(ext, "")
                lines.append(f"| {arch} | {ext} | {n} | {note} |")

    if stats.errors:
        lines += ["", "## 错误 / 异常", ""]
        for e in stats.errors[:200]:
            lines.append(f"- {e}")
        if len(stats.errors) > 200:
            lines.append(f"- …另有 {len(stats.errors) - 200} 条")

    review = [r for r in stats.items if r.status in ("needs_review", "failed")]
    if review:
        lines += ["", "## 需要人工复核的文件", "", "| 位置 | 文件 | 状态 | 原因 |", "|---|---|---|---|"]
        for r in review[:300]:
            reason = (r.message or "; ".join(r.warnings[:2]) or "").replace("|", "/")[:120]
            lines.append(f"| {r.archive} | {Path(r.inner).name} | {r.status} | {reason} |")
        if len(review) > 300:
            lines.append(f"| … | 另有 {len(review)-300} 个 | | |")

    (out_root / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "source": str(src_root),
        "output": str(out_root),
        "elapsed_seconds": round(elapsed, 1),
        "status": dict(stats.status),
        "type_totals": dict(stats.type_totals),
        "per_archive": {k: dict(v) for k, v in stats.per_archive.items()},
        "unprocessed_exts": {k: dict(v) for k, v in stats.unsupported.items()},
        "errors": stats.errors,
        "items": [
            {
                "archive": r.archive,
                "file": r.inner,
                "kind": r.kind,
                "status": r.status,
                "counts": r.counts,
                "warnings": r.warnings,
                "message": r.message,
            }
            for r in stats.items
        ],
        "previously_done": len(previous_done or ()),
    }
    (out_root / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="批量脱敏图纸目录/压缩包")
    ap.add_argument("source", help="源目录（只读，不会被修改）")
    ap.add_argument("-o", "--out", required=True, help="输出目录")
    ap.add_argument("--max-depth", type=int, default=int(cfg("batch", "max_nested_depth", default=2)))
    ap.add_argument("--max-files", type=int, default=int(cfg("batch", "max_files", default=0)))
    ap.add_argument("--only", nargs="*", help="只处理名字含这些子串的顶文件夹/压缩包")
    ap.add_argument("--exts", nargs="*", help="只处理这些扩展名（如 .pdf .xlsx）")
    ap.add_argument("--resume", action="store_true", help="跳过 report.json 里已成功处理的文件")
    args = ap.parse_args()

    src_root = Path(args.source).resolve()
    out_root = Path(args.out).resolve()
    if not src_root.exists():
        print(f"源目录不存在: {src_root}")
        return 2
    if out_root == src_root:
        print("输出目录不能与源目录相同（避免覆盖原件）")
        return 2
    out_root.mkdir(parents=True, exist_ok=True)

    only_exts = {e if e.startswith(".") else "." + e for e in args.exts} if args.exts else None

    stats = Stats(journal=out_root / "progress.jsonl")
    processed: list = []
    t0 = time.time()

    done_before: set[tuple[str, str]] = set()
    if args.resume:
        done_before = stats.replay(out_root / "progress.jsonl")
        if done_before:
            print(f"续跑：已跳过 {len(done_before)} 个已处理文件", flush=True)

    archives = sorted(
        (p for p in src_root.rglob("*")
         if p.is_file() and p.suffix.lower() in ARCHIVES),
        key=lambda p: p.stat().st_size,      # 小的先做：中断时已完成的多
    )
    loose = sorted(
        p for p in src_root.rglob("*")
        if p.is_file() and p.suffix.lower() not in ARCHIVES
    )

    print(f"发现 {len(archives)} 个压缩包，{len(loose)} 个散落文件", flush=True)
    for a in archives:
        print(f"  - {a.relative_to(src_root)}  ({a.stat().st_size/1024/1024:.1f} MB)", flush=True)

    for arch in archives:
        if args.only and not any(k in str(arch.relative_to(src_root)) for k in args.only):
            continue
        if args.max_files and len(processed) >= args.max_files:
            break
        rel = arch.relative_to(src_root)
        label = str(rel.parent / arch.stem) if arch.parent != src_root else arch.stem
        print(f"\n=== {rel} ({arch.stat().st_size/1024/1024:.1f} MB) ===", flush=True)
        out_dir = out_root / rel.parent / (arch.stem + "_redacted")
        try:
            with ArchiveReader(arch) as reader:
                handle_archive(
                    reader, label, out_dir, stats, 0, args.max_depth,
                    args.max_files, processed, only_exts, done_before,
                )
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"{rel}: 压缩包无法打开 -> {type(e).__name__}: {e}")
            print(f"  !! 压缩包无法打开: {type(e).__name__}: {e}", flush=True)

    if not args.only:
        for f in loose:
            if args.max_files and len(processed) >= args.max_files:
                break
            rel = f.relative_to(src_root)
            print(f"\n=== 散落文件 {rel} ===", flush=True)
            try:
                process_loose_file(f, out_root / rel.parent / ("redacted_" + f.name), stats, processed)
            except Exception as e:  # noqa: BLE001
                stats.errors.append(f"{rel}: {type(e).__name__}: {e}")

    elapsed = time.time() - t0
    write_report(stats, out_root, elapsed, src_root, done_before)
    stats.close()
    shutdown_pool()

    print("\n" + "=" * 60)
    print(f"完成：{len(stats.items)} 个文件，耗时 {elapsed/60:.1f} 分钟")
    print("状态：", dict(stats.status))
    print("命中：", dict(stats.type_totals))
    print(f"报告：{out_root / 'REPORT.md'}")
    return 0


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    sys.exit(main())
