"""T3.1 FastAPI 接口。

- POST /v1/redact            上传文件，返回 task_id
- GET  /v1/tasks/{id}        查询状态与命中统计
- GET  /v1/tasks/{id}/result 仅 done 时返回脱敏文件
- GET  /healthz              健康检查
- GET  /v1/reviews           待人工处理列表

修正点：
- `@app.on_event("startup")` 在新版 FastAPI 已弃用，改用 lifespan；
- 上传落盘改用项目内工作目录（原实现用系统 tempfile，受限环境会 PermissionError）；
- `/healthz` 暴露模型/OCR/转换后端状态，便于运维判断「到底有没有真正在脱敏」；
- 上传文件名做路径净化，避免 `../` 穿越到 clean 目录之外。
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from api import audit, storage, tasks
from core.config import cfg
from core.workdir import temporary_workdir
from handlers.dispatch import kind_of

MAX_UPLOAD = int(cfg("service", "max_upload_mb", default=50)) * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.ensure_dirs()
    audit.init_db()
    tasks.warmup()
    yield
    try:
        from handlers.dispatch import shutdown_pool

        shutdown_pool()
    except Exception:  # noqa: BLE001
        pass


app = FastAPI(title=cfg("service", "name", default="redact-service"), lifespan=lifespan)


def _safe_name(name: str) -> str:
    base = Path(name or "file").name
    return base.replace("\x00", "") or "file"


@app.get("/healthz")
def healthz() -> dict:
    from core.ner import ner_status
    from handlers.convert import backend_status

    ocr_ok: object = True
    try:
        from handlers.ocr import get_engine

        get_engine()
    except Exception as e:  # noqa: BLE001
        ocr_ok = f"{type(e).__name__}: {e}"
    return {
        "status": "ok",
        "ner": ner_status(),
        "ocr": ocr_ok,
        "convert": backend_status(),
        "storage": {"quarantine": str(storage.QUARANTINE), "clean": str(storage.CLEAN)},
    }


@app.post("/v1/redact")
async def redact(file: UploadFile = File(...)) -> dict:
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "文件超过大小限制")
    if not data:
        raise HTTPException(400, "空文件")

    filename = _safe_name(file.filename or "file")
    task_id = uuid.uuid4().hex
    with temporary_workdir("upload") as td:
        tmp = Path(td) / filename
        tmp.write_bytes(data)
        kind = kind_of(tmp)
    enc_path = storage.store_original(task_id, data)
    audit.create_task(task_id, filename, kind, str(enc_path))
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
        "filename": rec.get("filename"),
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
