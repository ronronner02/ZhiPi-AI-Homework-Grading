from __future__ import annotations

import base64
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from zhipi.config import Settings
from zhipi.main import create_app


def png_base64(width: int = 96, height: int = 72) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def upload_payload(**overrides) -> dict:
    payload = {
        "filename": "学生作业.png",
        "content_type": "image/png",
        "data_base64": png_base64(),
        "student_name": "测试学生",
        "class_id": "C001",
        "question_id": "Q001",
        "folder_id": "inbox",
    }
    payload.update(overrides)
    return payload


def test_health_seed_and_security_headers(client: TestClient):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").json()["status"] == "ready"
    response = client.get("/api/teacher/results")
    assert response.status_code == 200
    rows = response.json()["results"]
    assert len(rows) >= 10
    assert {row["status"] for row in rows if row.get("status")} >= {"green", "yellow"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-request-id"]


def test_access_code_login_and_logout(tmp_path: Path):
    settings = Settings.for_test(
        tmp_path,
        access_code="correct-horse",
        secret_key="a-test-secret-key-that-is-long-enough",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/config").status_code == 401
        assert client.post("/api/auth/login", json={"access_code": "wrong"}).status_code == 401
        login = client.post("/api/auth/login", json={"access_code": "correct-horse"})
        assert login.status_code == 200
        assert "httponly" in login.headers["set-cookie"].lower()
        assert "samesite=strict" in login.headers["set-cookie"].lower()
        assert client.get("/api/config").status_code == 200
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/config").status_code == 401


def test_review_validation_and_persistence(settings: Settings):
    with TestClient(create_app(settings)) as client:
        row = client.get("/api/teacher/results").json()["results"][0]
        submission_id = row["submission_id"]
        invalid_score = client.post(
            "/api/teacher/review",
            json={"submission_id": submission_id, "final_score": row["max_score"] + 1},
        )
        assert invalid_score.status_code == 422
        invalid_tag = client.post(
            "/api/teacher/review",
            json={"submission_id": submission_id, "final_error_tags": ["模型瞎猜"]},
        )
        assert invalid_tag.status_code == 422
        saved = client.post(
            "/api/teacher/review",
            json={
                "submission_id": submission_id,
                "teacher_action": "modified",
                "final_score": 1,
                "final_error_tags": ["计算错误", "计算错误"],
                "final_comment": "请复查关键计算。",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["review"]["final_error_tags"] == ["计算错误"]

    # 模拟服务重启：同一数据目录重新建 app，终审仍在 SQLite 中。
    with TestClient(create_app(settings)) as client:
        persisted = {
            row["submission_id"]: row
            for row in client.get("/api/teacher/results").json()["results"]
        }[submission_id]
        assert persisted["reviewed"] is True
        assert persisted["final_score"] == 1
        assert persisted["review"]["final_comment"] == "请复查关键计算。"


def test_upload_rejects_invalid_or_disguised_files(client: TestClient):
    bad_base64 = client.post(
        "/api/submissions/upload", json=upload_payload(data_base64="not!base64")
    )
    assert bad_base64.status_code == 400
    disguised = base64.b64encode(b"<script>alert(1)</script>").decode("ascii")
    bad_image = client.post(
        "/api/submissions/upload", json=upload_payload(data_base64=disguised)
    )
    assert bad_image.status_code == 400
    tiny = client.post(
        "/api/submissions/upload", json=upload_payload(data_base64=png_base64(1, 1))
    )
    assert tiny.status_code == 400
    traversal = client.get("/api/files/../../data/questions.json")
    assert traversal.status_code == 404


def test_upload_manual_fallback_file_access_and_reset(client: TestClient):
    response = client.post(
        "/api/submissions/upload",
        json=upload_payload(
            filename="../../成绩单.png",
            transcript="x²-5x+6=0，(x-2)(x-3)=0，x=2，x=3",
        ),
    )
    assert response.status_code == 201
    data = response.json()
    assert data["state"] == "manual_review"
    assert data["result"]["mode"] == "manual"
    assert data["result"]["status"] == "red"
    assert data["result"]["confidence"] == 0
    file_response = client.get(data["file_url"])
    assert file_response.status_code == 200
    assert file_response.headers["content-type"] == "image/jpeg"
    assert "no-store" in file_response.headers["cache-control"]
    rows = client.get("/api/submissions").json()["submissions"]
    uploaded = next(row for row in rows if row["submission_id"] == data["submission_id"])
    assert uploaded["student_name"] == "测试学生"
    assert uploaded["state"] == "manual_review"
    assert client.post("/api/recognize", json={"submission_id": data["submission_id"]}).status_code == 409
    reset = client.post("/api/demo/reset")
    assert reset.status_code == 200
    assert reset.json()["uploads_removed"] == 1
    assert client.get(data["file_url"]).status_code == 404


def test_upload_without_transcript_then_manual_grade(client: TestClient):
    created = client.post("/api/submissions/upload", json=upload_payload()).json()
    assert created["state"] == "awaiting_transcription"
    no_text = client.post("/api/grade", json={"submission_id": created["submission_id"]})
    assert no_text.status_code == 409
    graded = client.post(
        "/api/grade",
        json={"submission_id": created["submission_id"], "transcript": "学生手工作答"},
    )
    assert graded.status_code == 200
    assert graded.json()["mode"] == "manual"


def test_review_is_atomic_under_concurrent_writes(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as client:
        submission_id = client.get("/api/teacher/results").json()["results"][0]["submission_id"]
        service = app.state.service

        def write(score: int):
            return service.review(
                {
                    "submission_id": submission_id,
                    "teacher_action": "modified",
                    "final_score": score,
                    "final_error_tags": [],
                    "final_comment": str(score),
                    "final_dimension_scores": None,
                }
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(write, [0, 1, 2, 3]))
        assert len(records) == 4
        final = service.db.list_reviews(settings.workspace_id)[submission_id]
        assert final["final_score"] in {0, 1, 2, 3}


def test_body_limit_applies_before_json_parse(tmp_path: Path):
    settings = Settings.for_test(tmp_path, max_body_bytes=256)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/grade",
            content=b"x" * 1000,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 413


def test_quota_counter_is_persistent_and_atomic(settings: Settings):
    app = create_app(settings)
    with TestClient(app):
        database = app.state.service.db
        assert database.take_quota(settings.workspace_id, "vlm", 2) is True
        assert database.take_quota(settings.workspace_id, "vlm", 2) is True
        assert database.take_quota(settings.workspace_id, "vlm", 2) is False
        snapshot = database.quota_snapshot(settings.workspace_id, {"vlm": 2})
        assert snapshot["vlm"] == {
            "enabled": True,
            "limit": 2,
            "used": 2,
            "remaining": 0,
        }


def test_production_requires_auth_and_strong_secret(tmp_path: Path):
    base = Settings.for_test(tmp_path)
    values = {**base.__dict__, "environment": "production", "access_code": "", "secret_key": ""}
    with pytest.raises(ValueError, match="ZHIPI_ACCESS_CODE"):
        Settings(**values).validate()
