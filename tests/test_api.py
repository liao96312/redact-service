"""T3 接口端到端测试（同步模式）。

注意：接口层任务执行现在默认走**常驻工作进程池**，测试环境下改用同进程直调
（`REDACT_ISOLATE_PROCESS=0`），避免 pytest 里 spawn 子进程带来的路径/环境问题。
"""
import io
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("REDACT_ISOLATE_PROCESS", "0")
os.environ.setdefault("REDACT_DATA_HOME", str(Path(__file__).resolve().parent / ".runtime"))

from api.main import app  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "testdata"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _upload(client, name, data):
    return client.post("/v1/redact", files={"file": (name, io.BytesIO(data))})


def test_healthz(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_full_flow_docx(client):
    src = DATA / "docx" / "03.docx"
    if not src.exists():
        pytest.skip("testdata 未生成")
    data = src.read_bytes()
    r = _upload(client, "03.docx", data)
    assert r.status_code == 200
    tid = r.json()["task_id"]
    st = client.get(f"/v1/tasks/{tid}").json()
    assert st["status"] == "done", st
    assert st["counts"].get("PHONE", 0) >= 1
    res = client.get(f"/v1/tasks/{tid}/result")
    assert res.status_code == 200
    assert res.content


def test_reject_macro(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document/>")
        z.writestr("word/vbaProject.bin", b"MZ")
    r = _upload(client, "evil.docm", buf.getvalue())
    assert r.status_code == 200
    tid = r.json()["task_id"]
    st = client.get(f"/v1/tasks/{tid}").json()
    assert st["status"] == "failed"


def test_corrupt_pdf(client):
    r = _upload(client, "broken.pdf", b"%PDF-1.4 garbage not a pdf")
    tid = r.json()["task_id"]
    st = client.get(f"/v1/tasks/{tid}").json()
    assert st["status"] == "failed"


def test_result_only_when_done(client):
    r = _upload(client, "broken2.pdf", b"%PDF-1.4 garbage")
    tid = r.json()["task_id"]
    res = client.get(f"/v1/tasks/{tid}/result")
    assert res.status_code == 409


def test_missing_task(client):
    assert client.get("/v1/tasks/nope").status_code == 404


def test_upload_empty(client):
    r = _upload(client, "empty.txt", b"")
    assert r.status_code == 400


def test_pptx_now_supported(client):
    """pptx 原来被直接 fail，现在应能处理。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(
            "ppt/slides/slide1.xml",
            '<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
            ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>联系方式 13800138000</a:t></a:r></a:p>"
            "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>",
        )
    r = _upload(client, "deck.pptx", buf.getvalue())
    tid = r.json()["task_id"]
    st = client.get(f"/v1/tasks/{tid}").json()
    assert st["status"] == "done", st
    assert st["counts"].get("PHONE", 0) >= 1
