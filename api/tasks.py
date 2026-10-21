"""T3.1/T3.2 任务执行：Celery 异步（worker 启动时加载模型），或进程内同步。

process_task(task_id):
  解密原件 -> dispatch_redact -> 仅 done 时落盘 clean -> 更新状态 -> 审计

修正点：
- `status` 现在包含 `skipped`（缩略图缓存 / 图片行内噪声等无需处理的文件），
  这类不算失败，也不产生 clean 文件；
- 处理完立即清理隔离原件（可配置保留），避免图纸批处理把磁盘写满；
- 出 clean 文件时保留原始扩展名（原实现用 `out_<name>`，扩展名仍在，
  但上传名经过净化，避免路径穿越）。
"""
from __future__ import annotations

import time
from pathlib import Path

from api import audit, storage
from core.config import cfg
from core.workdir import temporary_workdir

SYNC_MODE = bool(cfg("service", "sync_mode", default=True))
KEEP_ORIGINAL = bool(cfg("storage", "keep_original", default=True))

try:
    from celery import Celery

    _app = Celery(
        "redact",
        broker=cfg("redis", "broker", default="redis://localhost:6379/0"),
        backend=cfg("redis", "backend", default="redis://localhost:6379/1"),
    )
    _app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_track_started=True,
    )
    if cfg("celery", "cpu_halved", default=True):
        import os

        _app.conf.worker_concurrency = max(1, (os.cpu_count() or 2) // 2)
    celery_app = _app
except Exception:  # pragma: no cover
    celery_app = None


def warmup() -> None:
    """worker/服务启动时加载模型一次。"""
    from core.ner import load_models

    load_models()
    try:
        from handlers.ocr import get_engine

        get_engine()
    except Exception:  # noqa: BLE001
        pass


def _process(task_id: str) -> None:
    from handlers.dispatch import dispatch_redact, kind_of

    rec = audit.get_task(task_id)
    if not rec:
        return
    audit.update_task(task_id, status="processing")
    start = time.time()
    enc_path = rec.get("enc_path")
    filename = Path(rec.get("filename") or "file").name
    operator = rec.get("operator") or ""

    with temporary_workdir("task") as td:
        try:
            data = storage.load_original(enc_path)
        except Exception:  # noqa: BLE001
            audit.update_task(task_id, status="failed", warnings=["原件解密失败"])
            audit.add_audit(task_id, filename, "", "failed", {}, int((time.time() - start) * 1000), operator)
            return
        in_path = Path(td) / filename
        in_path.write_bytes(data)
        kind = kind_of(in_path)
        out_path = Path(td) / ("redacted_" + filename)

        rep = dispatch_redact(in_path, out_path, "")
        duration = int((time.time() - start) * 1000)
        audit.update_task(task_id, status=rep.status, kind=kind, counts=rep.counts, warnings=rep.warnings)

        if rep.status == "done" and out_path.exists():
            storage.save_clean(task_id, filename, out_path.read_bytes())
        audit.add_audit(task_id, filename, kind, rep.status, rep.counts, duration, operator)

    if not KEEP_ORIGINAL:
        storage.purge_task(task_id)


if celery_app is not None:

    @celery_app.task(name="redact.process")
    def process_task(task_id: str) -> None:
        _process(task_id)

else:  # pragma: no cover

    def process_task(task_id: str) -> None:  # type: ignore
        _process(task_id)


def enqueue(task_id: str) -> None:
    if SYNC_MODE or celery_app is None:
        _process(task_id)
    else:
        process_task.delay(task_id)
