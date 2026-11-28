"""오류 응답: BE(Spring)와 같은 모양 {isSuccess:false, code, message, errors}으로, 사용자가 이해할 수 있는 한국어 문구."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import AppException, ErrorCode, install_handlers
from app.main import create_app
from tests.test_agent import fake_backend


def _client(tmp_path, **kw) -> TestClient:
    s = Settings(openai_api_key=None, openai_model="x", openai_base_url=None, llm_mask=True, mapper="rules",
                 spring_url="http://be", template_dir=str(tmp_path), max_upload_bytes=kw.pop("max_upload_bytes", 1024 * 1024),
                 agent_engine="workflow", ocr_engine="off", **kw)
    return TestClient(create_app(s, backend=fake_backend()[0]), raise_server_exceptions=False)


def _err(r, status, code):
    assert r.status_code == status, r.text
    body = r.json()
    assert body["isSuccess"] is False and body["code"] == code and body["message"]
    assert "Traceback" not in r.text and "Error(" not in body["message"]
    return body


def test_file_too_large_says_which_file_and_limit(tmp_path):
    c = _client(tmp_path)
    r = c.post("/v1/agent/sessions", files={"file": ("큰청구서.xlsx", b"0" * (1024 * 1024 + 10))})
    body = _err(r, 413, "FILE_TOO_LARGE")
    assert "큰청구서.xlsx" in body["message"] and "1MB" in body["message"]


def test_total_upload_too_large(tmp_path):
    c = _client(tmp_path, max_upload_bytes=600 * 1024, max_total_upload_bytes=1024 * 1024)
    files = [("files", (f"{i}.csv", b"0" * (400 * 1024))) for i in range(3)]
    body = _err(c.post("/v1/agent/sessions", files=files), 413, "UPLOAD_TOO_LARGE")
    assert "합쳐" in body["message"] and "1MB" in body["message"]


def test_no_file(tmp_path):
    _err(_client(tmp_path).post("/v1/agent/sessions", data={"terms": "3,5"}), 400, "FILE_REQUIRED")


def test_unsupported_file(tmp_path):
    body = _err(_client(tmp_path).post("/v1/agent/sessions", files={"file": ("a.hwp", b"xx")}), 400, "UNSUPPORTED_FILE")
    assert "xlsx" in body["message"].lower() or "지원" in body["message"]


def test_session_not_found(tmp_path):
    body = _err(_client(tmp_path).get("/v1/agent/sessions/nope"), 404, "SESSION_NOT_FOUND")
    assert "다시" in body["message"]


def test_request_body_validation_lists_fields(tmp_path):
    body = _err(_client(tmp_path).post("/v1/agent/sessions/nope/answer", json={}), 422, "VALIDATION_ERROR")
    assert body["message"] == "요청값을 확인해 주세요." and body["errors"][0]["field"] == "question_id"


def test_unexpected_error_hides_internals(tmp_path, monkeypatch):
    c = _client(tmp_path)
    import app.main as m
    monkeypatch.setattr(m, "extract", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret db password=xyz")))
    body = _err(c.post("/v1/agent/sessions", files={"file": ("a.xlsx", b"x")}), 400, "FILE_UNREADABLE")
    assert "secret" not in body["message"] and "a.xlsx" in body["message"]


def test_server_error_is_generic():
    app = FastAPI()
    install_handlers(app)

    @app.get("/boom")
    def boom():
        raise RuntimeError("internal detail 1009000000")

    body = _err(TestClient(app, raise_server_exceptions=False).get("/boom"), 500, "INTERNAL_ERROR")
    assert "1009000000" not in body["message"]


@pytest.mark.parametrize("code,status,message", [
    (ErrorCode.FORBIDDEN, 403, "권한이 없습니다."), (ErrorCode.UNAUTHORIZED, 401, "로그인이 필요합니다.")])
def test_app_exception_codes(code, status, message):
    app = FastAPI()
    install_handlers(app)

    @app.get("/x")
    def x():
        raise AppException(code)

    body = _err(TestClient(app).get("/x"), status, code.name)
    assert body["message"].startswith(message)


def test_legacy_detail_is_kept_for_old_clients(tmp_path):
    body = _err(_client(tmp_path).get("/v1/bills/nope"), 404, "NOT_FOUND")
    assert body["detail"] == "초안을 찾을 수 없습니다." and body["message"] == "초안을 찾을 수 없습니다."
