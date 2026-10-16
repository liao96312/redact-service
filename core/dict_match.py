"""T1.4 词典匹配（pyahocorasick + 热加载）。

词典来源：dicts/customers.txt（客户姓名、公司全称、简称，一行一个）。
命中类型按词条格式解析：支持 "词" 或 "词<TAB>类型"，默认 PERSON。
词长小于 2 的忽略。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from .config import cfg, rel
from .span import ORG, PERSON, Span

try:
    import ahocorasick

    _HAS_AHO = True
except Exception:  # pragma: no cover
    _HAS_AHO = False

_DICT_SCORE = float(cfg("score", "dict", default=0.95))
_MIN_LEN = int(cfg("limits", "min_dict_word_len", default=2))


class DictMatcher:
    def __init__(self, path: str | Path | None = None):
        p = path or cfg("paths", "dict_file", default="dicts/customers.txt")
        self.path = Path(p) if isinstance(p, str) and Path(p).is_absolute() else rel(str(p))
        self._lock = threading.Lock()
        self._automaton = None
        self._mtime = None
        self._entries: dict[str, str] = {}
        self.reload(force=True)

    def _parse(self, line: str) -> tuple[str, str] | None:
        line = line.strip()
        if not line or line.startswith("#"):
            return None
        typ = PERSON
        if "\t" in line:
            word, typ = line.split("\t", 1)
            word, typ = word.strip(), typ.strip().upper()
        else:
            word = line
        if len(word) < _MIN_LEN:
            return None
        return word, typ

    def _load(self) -> dict[str, str]:
        entries: dict[str, str] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                parsed = self._parse(line)
                if parsed:
                    entries[parsed[0]] = parsed[1]
        return entries

    def _build(self, entries: dict[str, str]):
        if not _HAS_AHO or not entries:
            return None
        auto = ahocorasick.Automaton()
        for word, typ in entries.items():
            auto.add_word(word, (word, typ))
        auto.make_automaton()
        return auto

    def reload(self, force: bool = False) -> bool:
        try:
            mtime = self.path.stat().st_mtime if self.path.exists() else None
        except OSError:
            mtime = None
        with self._lock:
            if not force and mtime is not None and mtime == self._mtime:
                return False
            self._entries = self._load()
            self._automaton = self._build(self._entries)
            self._mtime = mtime
        return True

    def maybe_hot_reload(self) -> None:
        """监测文件修改时间，变化则重载。"""
        try:
            mtime = self.path.stat().st_mtime if self.path.exists() else None
        except OSError:
            return
        if mtime is not None and mtime != self._mtime:
            self.reload(force=True)

    def search(self, text: str) -> list[Span]:
        if not text:
            return []
        self.maybe_hot_reload()
        with self._lock:
            auto = self._automaton
            entries = dict(self._entries)
        out: list[Span] = []
        if auto is not None:
            for end_idx, (word, typ) in auto.iter(text):
                start = end_idx - len(word) + 1
                out.append(Span(start, end_idx + 1, typ, _DICT_SCORE, "dict"))
        elif entries:
            # 无 pyahocorasick 时的退化为简单扫描
            for word, typ in entries.items():
                i = text.find(word)
                while i != -1:
                    out.append(Span(i, i + len(word), typ, _DICT_SCORE, "dict"))
                    i = text.find(word, i + 1)
        return out


_default: DictMatcher | None = None
_default_lock = threading.Lock()


def get_matcher() -> DictMatcher:
    global _default
    with _default_lock:
        if _default is None:
            _default = DictMatcher()
    return _default


def dict_spans(text: str) -> list[Span]:
    return get_matcher().search(text)


def reload_dict() -> bool:
    return get_matcher().reload(force=True)
