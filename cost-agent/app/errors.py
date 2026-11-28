"""오류 응답 — BE(Spring) ApiResponse와 같은 모양으로, 사용자가 이해할 수 있는 한국어 문구를 돌려준다.

    {"isSuccess": false, "code": "FILE_TOO_LARGE", "message": "…", "meta": {"requestId", "generatedAt"},
     "errors": [{"field", "reason"}], "detail": …}

- 코드에서 예상한 오류는 `raise AppException(ErrorCode.X, name=…)` — 문구는 여기 한 곳에서 관리한다
- 예전 `HTTPException`도 같은 모양으로 바꿔 준다. `detail`은 예전 클라이언트를 위해 그대로 둔다
- 예상하지 못한 오류는 내부 내용(스택·값)을 감추고 일반 문구만 보낸다 (로그에만 남김)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("cost-agent")


class ErrorCode(Enum):
    """(HTTP 상태, 기본 문구). 문구의 {이름}은 AppException(…, 이름=값)으로 채운다."""

    INVALID_REQUEST = (400, "요청 형식을 확인해 주세요.")
    FILE_REQUIRED = (400, "파일을 하나 이상 올려 주세요.")
    TOO_MANY_FILES = (400, "한 번에 최대 {max}개까지 올릴 수 있어요. 나눠서 올려 주세요.")
    UNSUPPORTED_FILE = (400, "{reason}")
    FILE_UNREADABLE = (400, "'{name}' 파일을 읽지 못했어요. 파일이 손상되지 않았는지, 암호가 걸려 있지 않은지 확인해 주세요.")
    INVALID_TERMS = (400, "계약 기간은 '3,5'처럼 숫자로 입력해 주세요.")
    UNAUTHORIZED = (401, "로그인이 필요합니다. 다시 로그인해 주세요.")
    FORBIDDEN = (403, "권한이 없습니다.")
    NOT_FOUND = (404, "요청한 대상을 찾을 수 없습니다.")
    SESSION_NOT_FOUND = (404, "작업을 찾을 수 없어요. 서버가 다시 시작되면 진행 중이던 작업이 사라질 수 있어요. 처음부터 다시 시작해 주세요.")
    REPORT_NOT_READY = (409, "아직 {what}가 준비되지 않았어요. 남은 단계를 마친 뒤 다시 시도해 주세요.")
    FILE_TOO_LARGE = (413, "'{name}' 파일이 너무 커요. 파일 하나는 최대 {mb}MB까지 올릴 수 있어요.")
    UPLOAD_TOO_LARGE = (413, "올린 파일을 합쳐 최대 {mb}MB까지 보낼 수 있어요. 파일을 나눠서 올려 주세요.")
    VALIDATION_ERROR = (422, "요청값을 확인해 주세요.")
    AGENT_INPUT = (422, "{reason}")
    BACKEND_UNAVAILABLE = (502, "원가 계산 서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요.")
    INTERNAL_ERROR = (500, "일시적인 오류가 발생했어요. 잠시 후 다시 시도해 주세요.")

    @property
    def status(self) -> int:
        return self.value[0]

    @property
    def template(self) -> str:
        return self.value[1]


class AppException(Exception):
    """예상한 오류. 문구는 ErrorCode에서 만든다 (message를 주면 그 문구를 쓴다)."""

    def __init__(self, code: ErrorCode, message: str | None = None, errors: list[dict] | None = None,
                 detail=None, **params):
        self.code = code
        try:
            self.message = message or code.template.format(**params)
        except (KeyError, IndexError):
            self.message = message or code.template
        self.errors = errors
        self.detail = detail
        super().__init__(self.message)


# HTTPException 상태 → 코드 (예전 코드 경로)
_BY_STATUS = {400: ErrorCode.INVALID_REQUEST, 401: ErrorCode.UNAUTHORIZED, 403: ErrorCode.FORBIDDEN, 404: ErrorCode.NOT_FOUND,
              409: ErrorCode.REPORT_NOT_READY, 413: ErrorCode.UPLOAD_TOO_LARGE, 422: ErrorCode.VALIDATION_ERROR,
              502: ErrorCode.BACKEND_UNAVAILABLE}


def body(code: str, message: str, errors: list[dict] | None = None, detail=None) -> dict:
    out = {"isSuccess": False, "code": code, "message": message,
           "meta": {"requestId": str(uuid.uuid4()), "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")},
           "detail": message if detail is None else detail}
    if errors:
        out["errors"] = errors
    return out


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def _app(_: Request, exc: AppException):
        return JSONResponse(body(exc.code.name, exc.message, exc.errors, exc.detail), status_code=exc.code.status)

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException):
        code = _BY_STATUS.get(exc.status_code, ErrorCode.INTERNAL_ERROR if exc.status_code >= 500 else ErrorCode.INVALID_REQUEST)
        d = exc.detail
        message = d if isinstance(d, str) else (d.get("message") if isinstance(d, dict) and d.get("message") else code.template)
        return JSONResponse(body(code.name, message, detail=d), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        errors = [{"field": ".".join(str(p) for p in e.get("loc", ()) if p not in ("body", "query", "path")),
                   "reason": "입력값이 유효하지 않습니다."} for e in exc.errors()]
        return JSONResponse(body("VALIDATION_ERROR", ErrorCode.VALIDATION_ERROR.template, errors), status_code=422)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        log.exception("처리하지 못한 오류")  # 내용은 로그에만, 응답에는 일반 문구
        return JSONResponse(body("INTERNAL_ERROR", ErrorCode.INTERNAL_ERROR.template), status_code=500)
