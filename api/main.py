"""T3.1 FastAPI 接口。

- POST /v1/redact            上传文件，返回 task_id
- GET  /v1/tasks/{id}        查询状态与命中统计
- GET  /v1/tasks/{id}/result 仅 done 时返回脱敏文件
- GET  /healthz              健康检查
- GET  /v1/reviews           待人工处理列表
"""
from __future__ import annotations

import uuid

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from api import audit, storage, tasks
from core.config import cfg
from handlers.dispatch import sniff

app = FastAPI(title=cfg("service", "name", default="redact-service"))
MAX_UPLOAD = int(cfg("service", "max_upload_mb", default=50)) * 1024 * 1024


@app.on_event("startup")
def _startup() -> None:
    storage.ensure_dirs()
    audit.init_db()
    tasks.warmup()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/redact")
async def redact(file: UploadFile = File(...)) -> dict:
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "文件超过大小限制")
    if not data:
        raise HTTPException(400, "空文件")
    task_id = uuid.uuid4().hex
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(delete=False) as tf:
        tf.write(data)
        tmp = Path(tf.name)
    kind = sniff(tmp)
    try:
        enc_path = storage.store_original(task_id, data)
    finally:
        tmp.unlink(missing_ok=True)
    audit.create_task(task_id, file.filename or "file", kind, str(enc_path))
    tasks.enqueue(task_id)
    return {"task_id": task_id, "status": audit.get_task(task_id)["status"]}


@app.get("/v1/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    rec = audit.get_task(task_id)
    if not rec:
        raise HTTPException(404, "任务不存在")
    return {
        "task_id": task_id,
        "status": rec["status"],
        "kind": rec["kind"],
        "counts": rec.get("counts", {}),
        "warnings": rec.get("warnings", []),
    }


@app.get("/v1/tasks/{task_id}/result")
def get_result(task_id: str):
    rec = audit.get_task(task_id)
    if not rec:
        raise HTTPException(404, "任务不存在")
    if rec["status"] != "done":
        raise HTTPException(409, f"任务状态为 {rec['status']}，不返回文件")
    p = storage.clean_result_path(task_id)
    if not p:
        raise HTTPException(404, "结果文件不存在")
    return FileResponse(str(p), filename=p.name)


@app.get("/v1/reviews")
def reviews() -> dict:
    return {"items": audit.list_review()}
