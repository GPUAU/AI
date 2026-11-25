"""환경변수 설정. 비밀값은 .env(로컬) 또는 Kubernetes Secret(배포)으로만 받는다."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """로컬 개발용 .env를 읽는다. 이미 설정된 환경변수(배포 Secret 등)는 덮어쓰지 않는다."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None
    openai_model: str
    openai_base_url: str | None  # 사내 sLLM(OpenAI 호환) 주소. 있으면 외부 OpenAI 대신 여기로 보낸다
    llm_mask: bool  # 금액·숫자·식별자 가림 (기본 켬)
    mapper: str  # auto | openai | rules
    spring_url: str
    template_dir: str
    max_upload_bytes: int
    agent_engine: str = "graph"  # graph(LangGraph 에이전트) | workflow(고정 순서)
    controller_model: str | None = None  # 다음 행동을 고르는 판단자 모델. 없으면 openai_model
    agent_controller: bool = True  # False면 그래프는 쓰되 판단은 기본 순서로
    checkpoint_db: str | None = None  # 세션 저장 SQLite 경로. 없으면 메모리

    @classmethod
    def from_env(cls) -> "Settings":
        load_env_file()
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            openai_base_url=os.getenv("OPENAI_BASE_URL") or None,
            llm_mask=os.getenv("LLM_MASK", "true").lower() not in ("0", "false", "off", "no"),
            mapper=os.getenv("MAPPER", "auto").lower(),
            spring_url=os.getenv("SPRING_URL", "http://backend"),
            template_dir=os.getenv("TEMPLATE_DIR", "./data/templates"),
            max_upload_bytes=int(float(os.getenv("MAX_UPLOAD_MB", "5")) * 1024 * 1024),
            agent_engine=os.getenv("AGENT_ENGINE", "graph").lower(),
            controller_model=os.getenv("CONTROLLER_MODEL") or None,
            agent_controller=os.getenv("AGENT_CONTROLLER", "on").lower() not in ("0", "false", "off", "no"),
            checkpoint_db=os.getenv("CHECKPOINT_DB") or None,
        )
