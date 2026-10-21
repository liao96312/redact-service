"""极简 OLE2（CFB）读取器 + BIFF8（.xls）文本提取。

为什么需要它：目标机没有 LibreOffice，Microsoft Office 又因「产品激活失败」无法
用 COM 自动化，内网也没有可离线安装的 xlrd/olefile。图纸包里却有 84 个 .xls
（包装单/标签），里面直接写着客户名、电话、地址，必须能读到。

实现范围（够用即止，不追求写回）：
- CFB：读 FAT / MiniFAT / 目录项，取名为 Workbook 或 Book 的流；
- BIFF8：逐条解析 record，提取 SST（共享字符串）、LABELSST、LABEL、NUMBER、
  RK、MULRK、FORMULA 的字符串结果、BOUNDSHEET（表名）；
- 输出：[Sheet(name, rows)]，供上层转成 xlsx 或直接做文本检测。

不支持：加密工作簿、BIFF5 及更早格式的部分编码（尽量按 latin-1 兜底）。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_REC = struct.Struct("<HH")

# BIFF 记录号
_BOF = 0x0809
_BOUNDSHEET = 0x0085
_SST = 0x00FC
_CONTINUE = 0x003C
_LABELSST = 0x00FD
_LABEL = 0x0204
_NUMBER = 0x0203
_RK = 0x027E
_MULRK = 0x00BD
_FORMULA = 0x0006
_STRING = 0x0207


class OLEError(RuntimeError):
    pass


@dataclass
class DirEntry:
    name: str
    kind: int          # 1=storage 2=stream 5=root
    start: int
    size: int


@dataclass
class CFBFile:
    data: bytes
    sector_size: int
    mini_size: int
    fat: list[int] = field(default_factory=list)
    minifat: list[int] = field(default_factory=list)
    entries: list[DirEntry] = field(default_factory=list)
    mini_stream: bytes = b""

    @classmethod
    def load(cls, data: bytes) -> "CFBFile":
        if len(data) < 512 or data[:8] != OLE2_MAGIC:
            raise OLEError("不是 OLE2 复合文档")
        sector_shift = struct.unpack_from("<H", data, 30)[0]
        mini_shift = struct.unpack_from("<H", data, 32)[0]
        sector_size = 1 << sector_shift
        mini_size = 1 << mini_shift
        num_fat = struct.unpack_from("<I", data, 44)[0]
        dir_start = struct.unpack_from("<I", data, 48)[0]
        mini_start = struct.unpack_from("<I", data, 60)[0]
        difat_start = struct.unpack_from("<I", data, 68)[0]
        num_difat = struct.unpack_from("<I", data, 72)[0]

        obj = cls(data=data, sector_size=sector_size, mini_size=mini_size)

        def sector_off(sec: int) -> int:
            return (sec + 1) * sector_size

        fat_sectors = list(struct.unpack_from("<109I", data, 76))[:num_fat]
        sec = difat_start
        guard = 0
        while sec < 0xFFFFFFFA and guard < num_difat and guard < 4096:
            guard += 1
            off = sector_off(sec)
            if off + sector_size > len(data):
                break
            vals = struct.unpack_from(f"<{sector_size // 4}I", data, off)
            fat_sectors.extend(v for v in vals[:-1] if v < 0xFFFFFFFA)
            sec = vals[-1]

        fat: list[int] = []
        for fs in fat_sectors:
            if fs >= 0xFFFFFFFA:
                continue
            off = sector_off(fs)
            if off + sector_size > len(data):
                continue
            fat.extend(struct.unpack_from(f"<{sector_size // 4}I", data, off))
        obj.fat = fat

        directory = obj._read_chain(dir_start)
        for i in range(0, len(directory), 128):
            entry = directory[i:i + 128]
            if len(entry) < 128:
                break
            nlen = struct.unpack_from("<H", entry, 64)[0]
            if nlen < 2 or nlen > 64:
                continue
            name = entry[:nlen - 2].decode("utf-16-le", "ignore")
            obj.entries.append(
                DirEntry(
                    name=name,
                    kind=entry[66],
                    start=struct.unpack_from("<I", entry, 116)[0],
                    size=struct.unpack_from("<Q", entry, 120)[0],
                )
            )

        if mini_start < 0xFFFFFFFA:
            mini = obj._read_chain(mini_start)
            count = len(mini) // 4
            obj.minifat = list(struct.unpack_from(f"<{count}I", mini, 0)) if count else []

        root = next((e for e in obj.entries if e.kind == 5), None)
        if root is not None and root.size and root.start < 0xFFFFFFFA:
            stream = obj._read_chain(root.start)
            obj.mini_stream = stream[: root.size or len(stream)]
        return obj

    def _read_chain(self, start: int) -> bytes:
        out = bytearray()
        sec = start
        guard = 0
        while sec < 0xFFFFFFFA and guard < 1_000_000:
            guard += 1
            off = (sec + 1) * self.sector_size
            if off + self.sector_size > len(self.data):
                break
            out += self.data[off: off + self.sector_size]
            if sec >= len(self.fat):
                break
            sec = self.fat[sec]
        return bytes(out)

    def _read_mini_chain(self, start: int, size: int) -> bytes:
        out = bytearray()
        sec = start
        guard = 0
        while sec < 0xFFFFFFFA and guard < 1_000_000:
            guard += 1
            off = sec * self.mini_size
            out += self.mini_stream[off: off + self.mini_size]
            if sec >= len(self.minifat):
                break
            sec = self.minifat[sec]
        return bytes(out[:size])

    def stream(self, names: tuple[str, ...]) -> bytes | None:
        wanted = {n.lower() for n in names}
        for e in self.entries:
            if e.kind == 2 and e.name.lower() in wanted:
                # 目录项声明的尺寸不合理（大于文件本身）说明文件损坏
                if e.size > len(self.data):
                    raise OLEError("目录项尺寸异常，文件可能损坏")
                if e.size < 4096:
                    blob = self._read_mini_chain(e.start, e.size)
                    if blob:
                        return blob
                return self._read_chain(e.start)[: e.size]
        return None


# ---------------------------------------------------------------- SST
class _ChunkReader:
    """SST 可能跨多个 CONTINUE；CONTINUE 的第一个字节是新的「宽窄」标志。"""

    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.ci = 0
        self.pos = 0
        self.wide = True

    def _cur(self) -> bytes:
        return self.chunks[self.ci]

    def at_end(self) -> bool:
        return self.ci >= len(self.chunks) or (
            self.ci == len(self.chunks) - 1 and self.pos >= len(self._cur())
        )

    def next_chunk(self) -> bool:
        if self.ci + 1 >= len(self.chunks):
            return False
        self.ci += 1
        self.pos = 0
        if self.pos < len(self._cur()):
            self.wide = bool(self._cur()[0] & 0x01)
            self.pos = 1
        return True

    def ensure(self, need: int) -> bool:
        while self.ci < len(self.chunks) and len(self._cur()) - self.pos < need:
            if not self.next_chunk():
                return False
        return self.ci < len(self.chunks)

    def take(self, n: int) -> bytes:
        out = b""
        while n > 0:
            if not self.ensure(1):
                break
            avail = len(self._cur()) - self.pos
            k = min(n, avail)
            out += self._cur()[self.pos: self.pos + k]
            self.pos += k
            n -= k
        return out

    def read_chars(self, count: int) -> str:
        parts: list[str] = []
        remaining = count
        while remaining > 0:
            if self.ci >= len(self.chunks):
                break
            avail_bytes = len(self._cur()) - self.pos
            char_size = 2 if self.wide else 1
            take = min(remaining, avail_bytes // char_size)
            if take <= 0:
                if not self.next_chunk():
                    break
                continue
            raw = self._cur()[self.pos: self.pos + take * char_size]
            parts.append(raw.decode("utf-16-le" if self.wide else "latin-1", "ignore"))
            self.pos += take * char_size
            remaining -= take
            if remaining > 0 and not self.next_chunk():
                break
        return "".join(parts)


def _parse_sst(chunks: list[bytes]) -> list[str]:
    if not chunks:
        return []
    head = chunks[0]
    if len(head) < 8:
        return []
    total = struct.unpack_from("<I", head, 4)[0]
    rd = _ChunkReader(chunks)
    rd.pos = 8
    strings: list[str] = []
    for _ in range(min(total, 1_000_000)):
        if not rd.ensure(3):
            break
        cch = struct.unpack_from("<H", rd._cur(), rd.pos)[0]
        flags = rd._cur()[rd.pos + 2]
        rd.pos += 3
        rich = 0
        ext = 0
        if flags & 0x08:
            if not rd.ensure(2):
                break
            rich = struct.unpack_from("<H", rd._cur(), rd.pos)[0]
            rd.pos += 2
        if flags & 0x04:
            if not rd.ensure(4):
                break
            ext = struct.unpack_from("<I", rd._cur(), rd.pos)[0]
            rd.pos += 4
        rd.wide = bool(flags & 0x01)
        text = rd.read_chars(cch)
        if rich:
            rd.take(rich * 4)
        if ext:
            rd.take(ext)
        strings.append(text)
    return strings


# ---------------------------------------------------------------- BIFF 主解析
@dataclass
class Sheet:
    name: str
    rows: list[list[str]] = field(default_factory=list)


def _sst_chunks(data: bytes, rec_end: int) -> list[bytes]:
    """收集 SST 本体 + 紧随其后的 CONTINUE 记录。"""
    chunks = [data[rec_end[0]: rec_end[1]]]
    pos = rec_end[1]
    while pos + 4 <= len(data):
        rid, length = _REC.unpack_from(data, pos)
        if rid != _CONTINUE:
            break
        chunks.append(data[pos + 4: pos + 4 + length])
        pos += 4 + length
    return chunks


def _parse_biff_stream(data: bytes) -> list[Sheet]:
    pos = 0
    n = len(data)
    sst: list[str] = []
    sheets_meta: list[tuple[str, int]] = []
    cells: dict[int, dict[tuple[int, int], str]] = {}
    current_sheet = -1
    pending_formula: tuple[int, int, int] | None = None   # (sheet, row, col)

    while pos + 4 <= n:
        rid, length = _REC.unpack_from(data, pos)
        body_start = pos + 4
        body = data[body_start: body_start + length]
        pos = body_start + length

        if rid == _BOUNDSHEET:
            try:
                cch = body[6]
                flags = body[7]
                if flags & 0x01:
                    nm = body[8: 8 + cch * 2].decode("utf-16-le", "ignore")
                else:
                    nm = body[8: 8 + cch].decode("latin-1", "ignore")
                sheets_meta.append((nm, struct.unpack_from("<I", body, 0)[0]))
            except Exception:  # noqa: BLE001
                continue
        elif rid == _SST:
            sst = _parse_sst(_sst_chunks(data, (body_start, pos)))
        elif rid == _BOF:
            if len(body) >= 4 and struct.unpack_from("<H", body, 2)[0] == 0x0010:
                current_sheet += 1
                cells.setdefault(current_sheet, {})
        elif rid == _LABELSST and len(body) >= 10:
            r, c, _xf, idx = struct.unpack_from("<HHHI", body, 0)
            if 0 <= idx < len(sst):
                cells.setdefault(current_sheet, {})[(r, c)] = sst[idx]
        elif rid == _LABEL and len(body) >= 9:
            r, c, _xf, cch = struct.unpack_from("<HHHH", body, 0)
            flags = body[8]
            if flags & 0x01:
                txt = body[9: 9 + cch * 2].decode("utf-16-le", "ignore")
            else:
                txt = body[9: 9 + cch].decode("latin-1", "ignore")
            cells.setdefault(current_sheet, {})[(r, c)] = txt
        elif rid == _NUMBER and len(body) >= 14:
            r, c, _xf = struct.unpack_from("<HHH", body, 0)
            val = struct.unpack_from("<d", body, 6)[0]
            cells.setdefault(current_sheet, {})[(r, c)] = _fmt_num(val)
        elif rid == _RK and len(body) >= 10:
            r, c, _xf = struct.unpack_from("<HHH", body, 0)
            rk = struct.unpack_from("<i", body, 6)[0]
            cells.setdefault(current_sheet, {})[(r, c)] = _fmt_num(_rk_to_double(rk))
        elif rid == _MULRK and len(body) >= 6:
            r, c0 = struct.unpack_from("<HH", body, 0)
            count = (len(body) - 6) // 6
            for i in range(max(0, count)):
                rk = struct.unpack_from("<i", body, 4 + i * 6 + 2)[0]
                cells.setdefault(current_sheet, {})[(r, c0 + i)] = _fmt_num(_rk_to_double(rk))
        elif rid == _FORMULA and len(body) >= 20:
            r, c, _xf = struct.unpack_from("<HHH", body, 0)
            res = body[6:14]
            if res[6:8] == b"\xff\xff":
                if res[0] == 0:      # 字符串结果，等下一条 STRING
                    pending_formula = (current_sheet, r, c)
                elif res[0] == 1:
                    cells.setdefault(current_sheet, {})[(r, c)] = "TRUE"
                elif res[0] == 2:
                    cells.setdefault(current_sheet, {})[(r, c)] = "FALSE"
            else:
                val = struct.unpack("<d", res)[0]
                cells.setdefault(current_sheet, {})[(r, c)] = _fmt_num(val)
        elif rid == _STRING and pending_formula is not None:
            sheet, r, c = pending_formula
            pending_formula = None
            if len(body) >= 3:
                cch = struct.unpack_from("<H", body, 0)[0]
                flags = body[2]
                if flags & 0x01:
                    txt = body[3: 3 + cch * 2].decode("utf-16-le", "ignore")
                else:
                    txt = body[3: 3 + cch].decode("latin-1", "ignore")
                cells.setdefault(sheet, {})[(r, c)] = txt

    out: list[Sheet] = []
    for idx, (name, _off) in enumerate(sheets_meta):
        out.append(Sheet(name=name, rows=_grid(cells.get(idx, {}))))
    if not out and cells:
        for idx, grid in sorted(cells.items()):
            out.append(Sheet(name=f"Sheet{idx + 1}", rows=_grid(grid)))
    return out


def _grid(cells: dict[tuple[int, int], str]) -> list[list[str]]:
    if not cells:
        return []
    max_r = max(r for r, _ in cells)
    max_c = max(c for _, c in cells)
    if max_r > 200_000 or max_c > 5_000:
        raise OLEError("表格尺寸异常，疑似损坏")
    return [[cells.get((r, c), "") for c in range(max_c + 1)] for r in range(max_r + 1)]


def _rk_to_double(rk: int) -> float:
    if rk & 0x02:
        val = float(rk >> 2)
    else:
        val = struct.unpack("<d", struct.pack("<Q", (rk & 0xFFFFFFFC) << 32))[0]
    if rk & 0x01:
        val /= 100.0
    return val


def _fmt_num(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return f"{v:g}"


def read_xls(path) -> list[Sheet]:
    """读取 .xls，返回 [Sheet(name, rows)]。"""
    from pathlib import Path

    data = Path(path).read_bytes()
    cfb = CFBFile.load(data)
    stream = cfb.stream(("Workbook", "Book"))
    if stream is None:
        raise OLEError("未找到 Workbook/Book 流（可能不是 Excel 文件）")
    return _parse_biff_stream(stream)
