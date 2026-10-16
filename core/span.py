"""T1.1 数据结构：Span 与类型常量。"""
from __future__ import annotations

from dataclasses import dataclass

PERSON = "PERSON"
ORG = "ORG"
ADDRESS = "ADDRESS"
PHONE = "PHONE"
BANK_CARD = "BANK_CARD"
ID_CARD = "ID_CARD"
EMAIL = "EMAIL"

TYPES = (PERSON, ORG, ADDRESS, PHONE, BANK_CARD, ID_CARD, EMAIL)


@dataclass
class Span:
    start: int
    end: int
    type: str
    score: float = 0.0
    source: str = ""

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("end < start")

    @property
    def length(self) -> int:
        return self.end - self.start

    def overlaps(self, other: "Span") -> bool:
        return self.start < other.end and other.start < self.end


# 兼容别名
Span.t = property(lambda self: self.type)
