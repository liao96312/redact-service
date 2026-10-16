"""T3 接口端到端测试（同步模式）。"""
import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app

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
    data = (DATA / "docx" / "03.docx").read_bytes()
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
