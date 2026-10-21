"""处理器公共：Report 结构 与 文本替换工具。"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.config import placeholder
from core.span import Span


@dataclass
class Report:
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    needs_review: bool = False
    status: str = "done"          # done | needs_review | failed | skipped
    message: str = ""
    degraded: str = ""            # 降级原因（如 pool_unavailable），不影响状态

    def add(self, type_name: str, n: int = 1) -> None:
        if n <= 0:
            return
        self.counts[type_name] = self.counts.get(type_name, 0) + n

    def add_spans(self, spans: list[Span]) -> None:
        for s in spans:
            self.add(s.type)

    def warn(self, msg: str, review: bool = False) -> None:
        if msg not in self.warnings:      # 去重：同一类告警不刷屏
            self.warnings.append(msg)
        if review:
            self.needs_review = True
            if self.status == "done":
                self.status = "needs_review"

    def fail(self, msg: str) -> "Report":
        self.status = "failed"
        self.message = msg
        return self

    def skip(self, msg: str) -> "Report":
        """无需处理的文件（缩略图缓存、CAD 二进制等），不算失败也不算成功。"""
        self.status = "skipped"
        self.message = msg
        return self

    def merge(self, other: "Report") -> "Report":
        """合并子处理器（内嵌图片等）的结果。"""
        for k, v in other.counts.items():
            self.add(k, v)
        for w in other.warnings:
            self.warn(w)
        if other.needs_review:
            self.needs_review = True
            if self.status == "done":
                self.status = "needs_review"
        return self

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in sorted(self.counts.items()) if v]
        return f"{self.status} ({', '.join(parts) or '无命中'})"

    def total(self) -> int:
        return sum(self.counts.values())


def redact_text(text: str, spans: list[Span]) -> str:
    """把 spans 按位置（从后往前）替换为占位符。"""
    if not spans:
        return text
    out = text
    for s in sorted(spans, key=lambda x: x.start, reverse=True):
        out = out[: s.start] + placeholder(s.type) + out[s.end:]
    return out
