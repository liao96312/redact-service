"""配置加载：从项目根 config.yaml 读取，单例缓存。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"


@lru_cache(maxsize=1)
def load_config(path: str | None = None) -> dict:
    p = Path(path or os.environ.get("REDACT_CONFIG") or CONFIG_PATH)
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    _apply_env_overrides(data)
    return data


def _apply_env_overrides(data: dict) -> None:
    """允许少量通过环境变量覆盖，便于 Docker 部署。"""
    sync = os.environ.get("REDACT_SYNC_MODE")
    if sync is not None:
        data.setdefault("service", {})["sync_mode"] = sync.strip().lower() in ("1", "true", "yes")
    if os.environ.get("REDACT_OCR_ENGINE"):
        data.setdefault("ocr", {})["engine"] = os.environ["REDACT_OCR_ENGINE"]


def cfg(*keys, default=None):
    node = load_config()
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def rel(path_str: str) -> Path:
    """把配置里的相对路径解析到项目根。"""
    p = Path(path_str)
    return p if p.is_absolute() else (ROOT / p)


def placeholder(type_name: str) -> str:
    ph = cfg("placeholders", default={}) or {}
    return ph.get(type_name, ph.get("DEFAULT", "[敏感信息]"))
