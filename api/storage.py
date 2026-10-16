"""T3.3 存储与加密。

- 上传文件先存隔离目录 quarantine
- 原件用 Fernet 加密保存（密钥仅来自环境变量 REDACT_KEY）
- 脱敏文件存 clean，仅此目录对主项目可见
- 定时清理超过 retention_days 的原件
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from core.config import cfg, rel

QUARANTINE = rel(cfg("paths", "quarantine_dir", default="/data/quarantine"))
CLEAN = rel(cfg("paths", "clean_dir", default="/data/clean"))
KEY_ENV = cfg("storage", "key_env", default="REDACT_KEY")
RETENTION_DAYS = int(cfg("storage", "retention_days", default=7))


def _load_key() -> bytes:
    key = os.environ.get(KEY_ENV)
    if key:
        return key.encode()
    # 开发兜底：本地生成并持久化，生产必须用环境变量
    fallback = Path(__file__).resolve().parent.parent / ".redact_key"
    if fallback.exists():
        return fallback.read_bytes().strip()
    key = Fernet.generate_key()
    fallback.write_bytes(key)
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
