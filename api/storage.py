"""T3.3 存储与加密。

- 上传文件先存隔离目录 quarantine
- 原件用 Fernet 加密保存（密钥仅来自环境变量 REDACT_KEY）
- 脱敏文件存 clean，仅此目录对主项目可见
- 定时清理超过 retention_days 的原件

修正点（Windows 上直接跑不起来的原因）：
- `paths.quarantine_dir: /data/quarantine` 在 Windows 会被 rel() 解析成
  `D:\\data\\quarantine`，普通账户无写权限 -> 上传即 PermissionError。
  改为 `data_path()`：绝对路径优先、不可写则回落到项目内 `.data/`；
- 密钥文件权限：`.redact_key` 写入后收紧 ACL（仅当前用户可读）；
- 新增 `purge_task()`，批处理结束后清理隔离区。
"""
from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from core.config import ROOT, cfg, data_path

QUARANTINE = data_path("quarantine_dir", "/data/quarantine")
CLEAN = data_path("clean_dir", "/data/clean")
KEY_ENV = cfg("storage", "key_env", default="REDACT_KEY")
RETENTION_DAYS = int(cfg("storage", "retention_days", default=7))


def _load_key() -> bytes:
    key = os.environ.get(KEY_ENV)
    if key:
        return key.encode()
    # 开发兜底：本地生成并持久化，生产必须用环境变量
    fallback = ROOT / ".redact_key"
    if fallback.exists():
        return fallback.read_bytes().strip()
    key = Fernet.generate_key()
    fallback.write_bytes(key)
    try:
        os.chmod(fallback, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    print(f"[WARN] {KEY_ENV} 未设置，已生成开发用临时密钥 {fallback}（生产禁止）")
    return key


def _fernet() -> Fernet:
    return Fernet(_load_key())


def ensure_dirs() -> None:
    QUARANTINE.mkdir(parents=True, exist_ok=True)
    CLEAN.mkdir(parents=True, exist_ok=True)


def store_original(task_id: str, data: bytes) -> Path:
    ensure_dirs()
    enc = _fernet().encrypt(data)
    p = QUARANTINE / f"{task_id}.bin"
    p.write_bytes(enc)
    return p


def load_original(enc_path) -> bytes:
    return _fernet().decrypt(Path(enc_path).read_bytes())


def save_clean(task_id: str, filename: str, data: bytes) -> Path:
    ensure_dirs()
    d = CLEAN / task_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / Path(filename).name
    p.write_bytes(data)
    return p


def clean_result_path(task_id: str) -> Path | None:
    d = CLEAN / task_id
    if not d.exists():
        return None
    files = [f for f in d.iterdir() if f.is_file()]
    return files[0] if files else None


def purge_task(task_id: str) -> None:
    """删除某个任务的隔离原件与产出（批处理结束后调用）。"""
    p = QUARANTINE / f"{task_id}.bin"
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
    shutil.rmtree(CLEAN / task_id, ignore_errors=True)


def cleanup_expired(days: int | None = None) -> int:
    """删除隔离目录中超过保留期的原件，返回删除数量。"""
    days = RETENTION_DAYS if days is None else days
    if not QUARANTINE.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for f in QUARANTINE.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed
