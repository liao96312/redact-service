"""配置加载：从项目根 config.yaml 读取，单例缓存。

修正点：
- `paths.*` 中的相对路径按项目根解析，绝对路径原样使用；并支持
  `REDACT_DATA_HOME` 把数据目录整体重定向（Windows 无 /data 时必需）；
- 新增 `ocr.max_side`、`ocr.word_box` 等真实语料需要的开关；
- 环境变量覆盖集中在一处，便于容器与本地开发共用一份 config.yaml。
"""
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
        data = yaml.safe_load(f) or {}
    _apply_env_overrides(data)
    return data


def _apply_env_overrides(data: dict) -> None:
    """允许少量通过环境变量覆盖，便于 Docker 部署。"""
    sync = os.environ.get("REDACT_SYNC_MODE")
    if sync is not None:
        data.setdefault("service", {})["sync_mode"] = sync.strip().lower() in ("1", "true", "yes")
    if os.environ.get("REDACT_OCR_ENGINE"):
        data.setdefault("ocr", {})["engine"] = os.environ["REDACT_OCR_ENGINE"]
    iso = os.environ.get("REDACT_ISOLATE_PROCESS")
    if iso is not None:
        data.setdefault("limits", {})["isolate_process"] = iso.strip().lower() in ("1", "true", "yes")
    # 批量离线处理时图纸/3D 方案 pptx 常超过接口默认的 50MB，
    # 允许用环境变量整体放宽（接口侧的上传限制仍由 service.max_upload_mb 控制）。
    max_mb = os.environ.get("REDACT_MAX_FILE_MB")
    if max_mb:
        try:
            data.setdefault("limits", {})["max_file_mb"] = float(max_mb)
        except ValueError:
            pass

    home = os.environ.get("REDACT_DATA_HOME")
    if home:
        paths = data.setdefault("paths", {})
        paths["quarantine_dir"] = str(Path(home) / "quarantine")
        paths["clean_dir"] = str(Path(home) / "clean")
        paths["audit_db"] = str(Path(home) / "audit.db")


def cfg(*keys, default=None):
    node = load_config()
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def rel(path_str: str) -> Path:
    """把配置里的相对路径解析到项目根；绝对路径保持原样。"""
    p = Path(path_str)
    return p if p.is_absolute() else (ROOT / p)


def data_path(key: str, default: str) -> Path:
    """读取 paths.* 配置并解析为可写路径；失败时回落到项目内 .data/。"""
    raw = cfg("paths", key, default=default) or default
    p = rel(str(raw))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        probe = p.parent / ".write_probe"
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
        return p
    except OSError:
        # 配置的目录不可写（例如 Windows 上的 /data）：回落到项目内 .data/
        fallback = ROOT / ".data" / key
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback


def placeholder(type_name: str) -> str:
    ph = cfg("placeholders", default={}) or {}
    return ph.get(type_name, ph.get("DEFAULT", "[敏感信息]"))
