"""T3.1/T3.2 任务执行：Celery 异步（worker 启动时加载模型），或进程内同步。

process_task(task_id):
  解密原件 -> dispatch_redact -> 仅 done 时落盘 clean -> 更新状态 -> 审计
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from core.config import cfg
from api import audit, storage

SYNC_MODE = bool(cfg("service", "sync_mode", default=True))

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
    except Exception:
        pass


def _process(task_id: str) -> None:
    from handlers.dispatch import dispatch_redact, sniff

    rec = audit.get_task(task_id)
    if not rec:
        return
    audit.update_task(task_id, status="processing")
    start = time.time()
    enc_path = rec.get("enc_path")
    filename = rec.get("filename") or "file"
    operator = rec.get("operator") or ""

    with tempfile.TemporaryDirectory() as td:
        try:
            data = storage.load_original(enc_path)
        except Exception:
            audit.update_task(task_id, status="failed", warnings=["原件解密失败"])
            audit.add_audit(task_id, filename, "", "failed", {}, int((time.time() - start) * 1000), operator)
            return
        in_path = Path(td) / Path(filename).name
        in_path.write_bytes(data)
        kind = sniff(in_path)
        out_path = Path(td) / ("out_" + Path(filename).name)

        rep = dispatch_redact(in_path, out_path, "")
        duration = int((time.time() - start) * 1000)
        audit.update_task(task_id, status=rep.status, kind=kind, counts=rep.counts, warnings=rep.warnings)

        if rep.status == "done" and out_path.exists():
            storage.save_clean(task_id, filename, out_path.read_bytes())
            audit.update_task(task_id, status="done")
        audit.add_audit(task_id, filename, kind, rep.status, rep.counts, duration, operator)


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
