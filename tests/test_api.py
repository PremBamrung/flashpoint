"""Integration tests using FastAPI TestClient with an in-memory SQLite DB."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

os.environ["API_TOKEN"] = "test-secret"
os.environ["STORAGE_ROOTS"] = "/tmp"

with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as _f:
    os.environ["DB_PATH"] = _f.name

from app.main import app  # noqa: E402

HEADERS = {"X-API-Token": "test-secret"}
BAD_HEADERS = {"X-API-Token": "wrong"}
BLAKE3 = "b" * 64


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_verify_rejects_bad_token(client):
    resp = client.post("/v1/verify", json={"blake3": BLAKE3, "size": 100}, headers=BAD_HEADERS)
    assert resp.status_code == 401


def test_verify_unknown_hash(client):
    resp = client.post("/v1/verify", json={"blake3": BLAKE3, "size": 100}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["result"] == "not_safe"


def test_verify_batch_empty(client):
    resp = client.post("/v1/verify/batch", json={"files": []}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_stats(client):
    resp = client.get("/v1/stats", headers=HEADERS)
    assert resp.status_code == 200
    assert "total_unique_files" in resp.json()


def test_reindex_and_poll(client):
    import time
    resp = client.post("/v1/admin/reindex", headers=HEADERS)
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    for _ in range(10):
        status_resp = client.get(f"/v1/jobs/{job_id}", headers=HEADERS)
        assert status_resp.status_code == 200
        if status_resp.json()["status"] in ("done", "error"):
            break
        time.sleep(0.5)


def test_content_not_found(client):
    resp = client.get(f"/v1/content/{BLAKE3}", headers=HEADERS)
    assert resp.status_code == 404


def test_verify_path_not_found(client):
    resp = client.post("/v1/verify/path", json={"path": "/nonexistent/file.jpg"}, headers=HEADERS)
    assert resp.status_code == 404


def test_verify_path_real_file(client, tmp_path):
    p = tmp_path / "sample.jpg"
    p.write_bytes(b"fake jpeg content" * 100)
    resp = client.post("/v1/verify/path", json={"path": str(p)}, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] == "safe"
    assert len(data["blake3"]) == 64
