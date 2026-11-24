"""환경변수 설정. 비밀값은 .env(로컬) 또는 Kubernetes Secret(배포)으로만 받는다."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None
    openai_model: str
    mapper: str  # auto | openai | rules
    spring_url: str
    template_dir: str
    max_upload_bytes: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            mapper=os.getenv("MAPPER", "auto").lower(),
            spring_url=os.getenv("SPRING_URL", "http://backend"),
            template_dir=os.getenv("TEMPLATE_DIR", "./data/templates"),
            max_upload_bytes=int(float(os.getenv("MAX_UPLOAD_MB", "5")) * 1024 * 1024),
        )
