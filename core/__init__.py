"""redact-service core 包。"""
from .detect import detect, merge
from .span import Span

__all__ = ["detect", "merge", "Span"]
