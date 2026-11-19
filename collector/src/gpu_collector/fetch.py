"""대용량 공개 파일 스트리밍 다운로드. 공통 처리 규칙 1절: 자동 재시도 없음, 불완전 파일은 정상 처리하지 않음."""
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import requests

USER_AGENT = "gpu-price-collector/1.0"
CHUNK_SIZE = 1024 * 1024


class FetchError(Exception):
    """이번 회차를 실패로 기록해야 하는 다운로드 오류."""


@dataclass
class FetchResult:
    url: str
    path: Path
    sha256: str
    size: int


def get_json(url, config):
    """작은 목록 파일을 받는다."""
    timeout = (config.get("connect_timeout_s", 15), config.get("read_idle_timeout_s", 60))
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    except requests.RequestException as error:
        raise FetchError(f"request_failed: {type(error).__name__}") from error
    if response.status_code != 200:
        raise FetchError(f"http_{response.status_code}")
    try:
        return response.json()
    except ValueError as error:
        raise FetchError("invalid_json") from error


def download(url, destination, config):
    connect = config.get("connect_timeout_s", 15)
    read_idle = config.get("read_idle_timeout_s", 60)
    total_limit = config.get("total_timeout_s", 600)
    started = time.monotonic()
    digest = hashlib.sha256()
    size = 0
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    try:
        with requests.get(url, headers=headers, stream=True, timeout=(connect, read_idle)) as response:
            if response.status_code != 200:
                # 403/429는 회차를 중단하고 우회하지 않는다.
                raise FetchError(f"http_{response.status_code}")
            expected = response.headers.get("Content-Length")
            with open(destination, "wb") as output:
                for chunk in response.iter_content(CHUNK_SIZE):
                    if time.monotonic() - started > total_limit:
                        raise FetchError("total_timeout")
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
    except requests.RequestException as error:
        raise FetchError(f"request_failed: {type(error).__name__}") from error
    if expected is not None and int(expected) != size:
        raise FetchError(f"incomplete_file: expected {expected}, got {size}")
    if size == 0:
        raise FetchError("empty_file")
    return FetchResult(url=url, path=Path(destination), sha256=digest.hexdigest(), size=size)
