"""LangSmith 추적: 켜도 사내 민감정보(금액·회사·견적번호·사업장)는 가린 채로만 나간다."""

import gzip
import json

import pytest
import requests

from app import tracing


SECRET = ["1,009,000,000", "1009000000", "솔빛컴퓨팅", "SIM-261126-02", "울산", "72,000,000", "72000000"]


def test_mask_payload_hides_amounts_vendor_quote_and_site():
    data = {"session": {"draft": {"amount": 1009000000.0, "file": "01_구매팀.pdf",
                                  "note": "솔빛컴퓨팅(가상 업체) 견적 SIM-261126-02 · 울산 · 72,000,000원"},
                        "flags": [True, None], "step": "MISSING"}}
    out = json.dumps(tracing.mask_payload(data), ensure_ascii=False)
    assert not any(s in out for s in SECRET)
    assert '"step": "MISSING"' in out and "true" in out  # 키·글자 구조·참거짓은 남는다


def test_tracing_is_off_without_api_key(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert tracing.from_env() is None


def test_automatic_unmasked_tracing_is_disabled(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    t = tracing.from_env()
    assert t is not None and t.project == "gpuau-cost-agent"
    import os
    # LangChain이 환경변수로 만드는 '가리지 않는' 기본 추적기는 끈다 — 이 앱이 붙이는 가린 추적기만 쓴다
    assert os.getenv("LANGSMITH_TRACING") is None and os.getenv("LANGCHAIN_TRACING_V2") is None


class _Capture(requests.adapters.BaseAdapter):
    def __init__(self):
        super().__init__()
        self.bodies: list[str] = []

    def send(self, request, **kw):
        body = request.body or b""
        if isinstance(body, str):
            body = body.encode()
        if request.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        self.bodies.append(body.decode("utf-8", "replace"))
        r = requests.Response()
        r.status_code, r._content, r.request = 200, b"{}", request
        r.headers["Content-Type"] = "application/json"
        return r

    def close(self):
        pass


@pytest.fixture
def captured_tracing(monkeypatch):
    cap = _Capture()
    t = tracing.Tracing.create(api_key="test-key", api_url="http://ls.test", project="test", batch=False)
    t.client.session.mount("http://", cap)  # 클라이언트가 자기 어댑터를 붙인 뒤에 가로채기
    t.client.session.mount("https://", cap)
    monkeypatch.setattr(tracing, "_current", t)
    return t, cap


def test_graph_run_is_traced_with_masked_payloads(captured_tracing, tmp_path, dept_docs):
    from app.agent.graph import GraphCostAgent
    from app.agent.models import Answer, Goal
    from app.mapping.rules import RulesMapper
    from app.mapping.templates import TemplateStore
    from app.service import BillService
    from tests.test_agent import fake_backend
    t, cap = captured_tracing
    agent = GraphCostAgent(BillService(RulesMapper(), TemplateStore(tmp_path)), fake_backend()[0])
    s = agent.start_many(list(dept_docs.values()), Goal(terms_years=[]))
    s = agent.answer(s.id, Answer(question_id=s.question.id, values={}))
    t.flush()
    sent = "\n".join(cap.bodies)
    assert cap.bodies and "\"name\":\"cost-agent\"" in sent and "analyze_bill" in sent  # 실행·도구 이름은 기록됨
    assert not any(x in sent for x in SECRET), [x for x in SECRET if x in sent]
