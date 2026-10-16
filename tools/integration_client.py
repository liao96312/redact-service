"""主项目接入示例（T3.5）：上传 -> 轮询 -> 取回脱敏文件。

依赖 requests。用法示例：
    python tools/integration_client.py http://127.0.0.1:8080 sample.docx
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests


def redact(base: str, path: str, poll: float = 1.0, timeout: float = 600.0) -> dict:
    p = Path(path)
    with open(p, "rb") as f:
        r = requests.post(f"{base}/v1/redact", files={"file": (p.name, f)}, timeout=60)
    r.raise_for_status()
    tid = r.json()["task_id"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        st = requests.get(f"{base}/v1/tasks/{tid}", timeout=30).json()
        if st["status"] in ("done", "needs_review", "failed"):
            st["download"] = f"{base}/v1/tasks/{tid}/result"
            return st
        time.sleep(poll)
    return {"task_id": tid, "status": "timeout"}


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
    src = sys.argv[2] if len(sys.argv) > 2 else "testdata/docx/01.docx"
    res = redact(base, src)
    print(res)
    if res.get("status") == "done":
        data = requests.get(res["download"], timeout=60).content
        out = Path(src).with_name("clean_" + Path(src).name)
        out.write_bytes(data)
        print(f"已保存脱敏文件: {out}")
    elif res.get("status") == "needs_review":
        print("需人工复核，不予放行到业务流程")
    elif res.get("status") == "failed":
        print("处理失败，请重传")
