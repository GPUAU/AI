"""원가 에이전트 API.

POST /v1/bills                 청구서 엑셀 업로드 → 매핑 초안 (AI 또는 템플릿)
GET  /v1/bills/{id}            초안 조회
PUT  /v1/bills/{id}/lines      담당자 수정·확정 (확정하면 양식 템플릿 저장)
POST /v1/bills/{id}/simulate   확정 원가 + 담당자 입력 → BE 원가 계산 API 호출
GET  /v1/templates             저장된 양식 템플릿 목록
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, File, Form, HTTPException, Response, UploadFile

from .agent.models import AgentSession, Answer, Goal
from .agent.graph import build_agent, checkpointer_from, openai_controller
from .agent.orchestrator import AgentError
from .agent.report import build_report

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


def build_controller(settings: Settings, service: BillService):
    """다음 행동을 고르는 LLM 판단자. AI 분류를 쓰는 환경에서만 켠다 (금액 없는 관찰만 보낸다)."""
    if settings.agent_engine != "graph" or not settings.agent_controller or not hasattr(service.mapper, "mask"):
        return None
    return openai_controller(settings.openai_api_key or "", settings.controller_model or settings.openai_model,
                             base_url=settings.openai_base_url)


def create_app(settings: Settings | None = None, service: BillService | None = None,
               backend: BackendClient | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = service or build_service(settings)
    backend = backend or BackendClient(settings.spring_url)
    app = FastAPI(title="GPU AU Cost Agent", version="0.2.0")
    controller = build_controller(settings, service)
    agent = build_agent(settings.agent_engine, service, backend, controller, checkpointer_from(settings.checkpoint_db))

    def svc() -> BillService:
        return service

    @app.get("/health")
    def health():
        return {"status": "ok", "mapper": service.mapper.name,
                "llm_masked": getattr(service.mapper, "mask", None),
                "engine": settings.agent_engine, "controller": "llm" if controller else "policy"}

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

    # ------------------------------------------------------------ 원가 에이전트 (대화형)
    @app.post("/v1/agent/sessions", response_model=AgentSession)
    async def agent_start(file: UploadFile = File(...), model_id: str | None = Form(None),
                          gpu_count: int | None = Form(None), terms: str = Form("3,5")):
        data = await file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(413, "파일이 너무 큽니다.")
        try:
            doc = extract(file.filename or "upload.xlsx", data)
            years = [int(x) for x in terms.split(",") if x.strip()]
        except UnsupportedFile as exc:
            raise HTTPException(400, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, "계약 기간은 '3,5'처럼 숫자로 입력하세요.") from exc
        try:
            return agent.start(doc, Goal(model_id=model_id or None, gpu_count=gpu_count, terms_years=years))
        except AgentError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/v1/agent/sessions/{session_id}", response_model=AgentSession)
    def agent_get(session_id: str):
        try:
            return agent.get(session_id)
        except KeyError as exc:
            raise HTTPException(404, "세션을 찾을 수 없습니다.") from exc

    @app.post("/v1/agent/sessions/{session_id}/answer", response_model=AgentSession)
    def agent_answer(session_id: str, ans: Answer):
        try:
            return agent.answer(session_id, ans)
        except KeyError as exc:
            raise HTTPException(404, "세션을 찾을 수 없습니다.") from exc
        except AgentError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/v1/agent/sessions/{session_id}/report.xlsx")
    def agent_report(session_id: str):
        try:
            s = agent.get(session_id)
        except KeyError as exc:
            raise HTTPException(404, "세션을 찾을 수 없습니다.") from exc
        if not s.report_ready:
            raise HTTPException(409, "아직 근거서가 준비되지 않았습니다.")
        return Response(build_report(s), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="cost-report-{session_id}.xlsx"'})

    @app.get("/v1/templates")
    def templates(s: BillService = Depends(svc)):
        return [{"fingerprint": t.fingerprint, "file_name": t.file_name, "vendor": t.vendor,
                 "rules": len(t.rules)} for t in s.templates.list()]

    return app


app = None  # uvicorn "app.main:create_app" --factory 로 실행
