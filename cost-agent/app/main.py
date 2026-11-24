"""원가 에이전트 API.

POST /v1/bills                 청구서 엑셀 업로드 → 매핑 초안 (AI 또는 템플릿)
GET  /v1/bills/{id}            초안 조회
PUT  /v1/bills/{id}/lines      담당자 수정·확정 (확정하면 양식 템플릿 저장)
POST /v1/bills/{id}/simulate   확정 원가 + 담당자 입력 → BE 원가 계산 API 호출
GET  /v1/templates             저장된 양식 템플릿 목록
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile

from .be_client import BackendClient, BackendError, build_request
from .config import Settings
from .mapping.llm import LLMMapper, openai_completion
from .mapping.rules import RulesMapper
from .mapping.templates import TemplateStore
from .models import BillDraft, CorrectionRequest, IssueLevel, SimulationInput
from .parsing import UnsupportedFile, extract
from .service import BillService


def build_service(settings: Settings) -> BillService:
    has_llm = bool(settings.openai_api_key or settings.openai_base_url)
    use_llm = settings.mapper == "openai" or (settings.mapper == "auto" and has_llm)
    if use_llm:
        if not has_llm:
            raise RuntimeError("MAPPER=openai 이면 OPENAI_API_KEY 또는 OPENAI_BASE_URL(사내 sLLM)이 필요합니다.")
        destination = f"사내 LLM ({settings.openai_base_url})" if settings.openai_base_url else f"OpenAI ({settings.openai_model})"
        mapper = LLMMapper(openai_completion(settings.openai_api_key or "", settings.openai_model,
                                             base_url=settings.openai_base_url),
                           mask=settings.llm_mask, destination=destination)
    else:
        mapper = RulesMapper()
    return BillService(mapper, TemplateStore(settings.template_dir))


def create_app(settings: Settings | None = None, service: BillService | None = None,
               backend: BackendClient | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = service or build_service(settings)
    backend = backend or BackendClient(settings.spring_url)
    app = FastAPI(title="GPU AU Cost Agent", version="0.1.0")

    def svc() -> BillService:
        return service

    @app.get("/health")
    def health():
        return {"status": "ok", "mapper": service.mapper.name,
                "llm_masked": getattr(service.mapper, "mask", None)}

    @app.post("/v1/bills", response_model=BillDraft)
    async def upload(file: UploadFile = File(...), s: BillService = Depends(svc)):
        data = await file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(413, "파일이 너무 큽니다.")
        try:
            doc = extract(file.filename or "upload.xlsx", data)
        except UnsupportedFile as exc:
            raise HTTPException(400, str(exc)) from exc
        return s.ingest(doc)

    @app.get("/v1/bills/{draft_id}", response_model=BillDraft)
    def get_bill(draft_id: str, s: BillService = Depends(svc)):
        try:
            return s.get(draft_id)
        except KeyError as exc:
            raise HTTPException(404, "초안을 찾을 수 없습니다.") from exc

    @app.put("/v1/bills/{draft_id}/lines", response_model=BillDraft)
    def correct(draft_id: str, req: CorrectionRequest, s: BillService = Depends(svc)):
        try:
            s.get(draft_id)
        except KeyError as exc:
            raise HTTPException(404, "초안을 찾을 수 없습니다.") from exc
        try:
            return s.correct(draft_id, req)
        except KeyError as exc:
            raise HTTPException(400, f"문서에 없는 줄입니다: {exc}") from exc

    @app.post("/v1/bills/{draft_id}/simulate")
    def simulate(draft_id: str, inp: SimulationInput, s: BillService = Depends(svc)):
        try:
            draft = s.get(draft_id)
        except KeyError as exc:
            raise HTTPException(404, "초안을 찾을 수 없습니다.") from exc
        errors = [i for i in draft.issues if i.level == IssueLevel.ERROR]
        if errors:
            raise HTTPException(422, {"message": "오류를 먼저 고쳐야 계산할 수 있습니다.",
                                      "issues": [e.model_dump() for e in errors]})
        try:
            request = build_request(draft.cost_items, inp)
            result = backend.simulate(request)
        except BackendError as exc:
            raise HTTPException(502 if exc.status is None else 422,
                                {"message": str(exc), "errors": exc.errors}) from exc
        return {"request": request, "result": result,
                "warnings": [i.model_dump() for i in draft.issues if i.level != IssueLevel.INFO]}

    @app.get("/v1/templates")
    def templates(s: BillService = Depends(svc)):
        return [{"fingerprint": t.fingerprint, "file_name": t.file_name, "vendor": t.vendor,
                 "rules": len(t.rules)} for t in s.templates.list()]

    return app


app = None  # uvicorn "app.main:create_app" --factory 로 실행
