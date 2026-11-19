"""원천 HTTP 조회와 대용량 공개 파일 스트리밍 다운로드. 불완전 파일은 정상 처리하지 않는다.

일시적 오류(접속 실패·시간 초과, 429·5xx)는 짧게 몇 번 다시 시도한다(send). 서버가 Retry-After를 주면 그만큼
기다린다. 403 등 거부 응답은 우회하지 않고 바로 돌려준다.
"""
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import requests

USER_AGENT = "gpu-price-collector/1.0"
CHUNK_SIZE = 1024 * 1024
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
ATTEMPTS = 3                 # 처음 1번 + 다시 시도 2번
BACKOFF_S = 2                # 2초, 4초 …
MAX_WAIT_S = 60
_sleep = time.sleep          # 테스트에서 바꿔 끼운다


class FetchError(Exception):
    """이번 회차를 실패로 기록해야 하는 다운로드 오류."""


def send(call, *, attempts=ATTEMPTS, sleep=None):
    """call()(HTTP 응답을 돌려주는 함수)을 일시적 오류일 때 다시 시도한다. 반환: 마지막 응답.

    마지막 시도까지 접속 실패·시간 초과면 그 예외(requests.RequestException)를 그대로 올린다."""
    sleep = sleep or _sleep
    for attempt in range(1, attempts + 1):
        try:
            response = call()
        except (requests.ConnectionError, requests.Timeout):
            if attempt == attempts:
                raise
            sleep(_backoff(attempt))
            continue
        if response.status_code not in RETRY_STATUS or attempt == attempts:
            return response
        wait = _retry_after(response) or _backoff(attempt)
        response.close()
        sleep(wait)
    raise AssertionError("unreachable")


def _backoff(attempt):
    return min(BACKOFF_S * 2 ** (attempt - 1), MAX_WAIT_S)


def _retry_after(response):
    value = response.headers.get("Retry-After")
    if value and value.strip().isdigit():
        return min(int(value.strip()), MAX_WAIT_S)
    return None


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
        response = send(lambda: requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout))
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
        with send(lambda: requests.get(url, headers=headers, stream=True, timeout=(connect, read_idle))) as response:
            if response.status_code != 200:
                # 다시 시도 후에도 429·5xx이거나 403 등 거부면 회차를 중단하고 우회하지 않는다.
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
