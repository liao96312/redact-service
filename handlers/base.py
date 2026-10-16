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
    status: str = "done"          # done | needs_review | failed
    message: str = ""

    def add(self, type_name: str, n: int = 1) -> None:
        self.counts[type_name] = self.counts.get(type_name, 0) + n

    def add_spans(self, spans: list[Span]) -> None:
        for s in spans:
            self.add(s.type)

    def warn(self, msg: str, review: bool = False) -> None:
        self.warnings.append(msg)
        if review:
            self.needs_review = True
            self.status = "needs_review"

    def fail(self, msg: str) -> "Report":
        self.status = "failed"
        self.message = msg
        return self

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
