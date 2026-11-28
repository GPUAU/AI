"""LangSmith 추적 (선택). 켜도 사내 민감정보는 가린 채로만 나간다.

켜는 법 (.env 또는 Secret):
    LANGSMITH_TRACING=true
    LANGSMITH_API_KEY=...            # 없으면 꺼진 채로 둔다
    LANGSMITH_PROJECT=gpuau-cost-agent (기본)
    LANGSMITH_ENDPOINT=...           # 자체 호스팅·EU 리전이면

원칙
- LangChain이 환경변수만 보고 붙이는 '가리지 않는' 기본 추적기는 끈다 (LANGSMITH_TRACING·LANGCHAIN_TRACING_V2를 지움).
  대신 이 앱이 직접 붙이는 추적기만 쓰고, 그 추적기의 LangSmith 클라이언트가 보내기 전에 모든 입력·출력·메타데이터를 가린다.
- 가림: 숫자 값 → "#NUM", 글자 속 숫자 → "#", 회사명·견적번호·사업장·이메일·URL → 표시어 (privacy.mask_text와 같음).
  그래서 추적 화면에서는 '어느 단계·도구를 어떤 순서로 불렀고 얼마나 걸렸는지'를 보고, 금액은 보지 못한다.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from typing import Any

from .privacy import mask_text, sensitive_terms

_TRUTHY = ("1", "true", "on", "yes")
_AUTO_ENV = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")
DEFAULT_PROJECT = "gpuau-cost-agent"


def mask_payload(value: Any, _terms: list[str] | None = None) -> Any:
    """추적 데이터 전체를 재귀로 가린다. 키는 그대로 두고 값만 바꾼다."""
    terms = sensitive_terms() if _terms is None else _terms
    if isinstance(value, dict):
        return {k: mask_payload(v, terms) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [mask_payload(v, terms) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return "#NUM"
    if isinstance(value, str):
        return mask_text(value, terms)
    if hasattr(value, "model_dump"):
        return mask_payload(value.model_dump(mode="json"), terms)
    return mask_text(str(value), terms)


@dataclass
class Tracing:
    client: Any
    tracer: Any
    project: str

    @classmethod
    def create(cls, api_key: str, project: str = DEFAULT_PROJECT, api_url: str | None = None,
               session=None, batch: bool = True) -> "Tracing":
        from langchain_core.tracers.langchain import LangChainTracer
        from langsmith import Client

        kw = {"session": session} if session is not None else {}
        client = Client(api_key=api_key, api_url=api_url, auto_batch_tracing=batch,
                        hide_inputs=mask_payload, hide_outputs=mask_payload, hide_metadata=mask_payload, **kw)
        return cls(client=client, tracer=LangChainTracer(client=client, project_name=project), project=project)

    def callbacks(self) -> list:
        return [self.tracer]

    def wrap_openai(self, oai_client):
        """OpenAI 호출도 같은 (가리는) 클라이언트로 기록한다. 보내는 프롬프트는 원래도 가린 사본이다."""
        from langsmith.wrappers import wrap_openai
        return wrap_openai(oai_client, tracing_extra={"client": self.client, "project_name": self.project})

    @contextlib.contextmanager
    def span(self, name: str, inputs: dict | None = None):
        """이 안의 실행(OpenAI 호출 등)을 가린 클라이언트로 묶어 기록한다. 환경변수 자동 추적과 무관하게 켠다."""
        import langsmith
        with langsmith.tracing_context(enabled=True, client=self.client, project_name=self.project), \
                langsmith.trace(name, run_type="chain", inputs=inputs or {}, client=self.client, project_name=self.project):
            yield

    def flush(self) -> None:
        with contextlib.suppress(Exception):
            self.tracer.wait_for_futures()
        with contextlib.suppress(Exception):
            self.client.flush()


_current: Tracing | None = None


def from_env() -> Tracing | None:
    """환경변수로 켠다. 가리지 않는 자동 추적은 언제나 끈다."""
    wanted = any(os.getenv(k, "").lower() in _TRUTHY for k in _AUTO_ENV)
    for k in _AUTO_ENV:
        os.environ.pop(k, None)
    key = os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")
    if not wanted or not key:
        return None
    return Tracing.create(api_key=key, project=os.getenv("LANGSMITH_PROJECT") or DEFAULT_PROJECT,
                          api_url=os.getenv("LANGSMITH_ENDPOINT") or None)


def init() -> Tracing | None:
    global _current
    _current = from_env()
    return _current


def current() -> Tracing | None:
    return _current


def span(name: str, inputs: dict | None = None):
    t = current()
    return t.span(name, inputs) if t else contextlib.nullcontext()


def wrap_openai(oai_client):
    t = current()
    return t.wrap_openai(oai_client) if t else oai_client
