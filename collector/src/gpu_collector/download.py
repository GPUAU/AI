"""파일 다운로드 캐시. 같은 경로에 이미 받은 파일이 있으면 다시 받지 않는다."""
import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Fetched:
    url: str
    path: Path
    sha256: str
    size: int
    cached: bool = False


def cached_download(url, target, config, downloader):
    """버전 경로별 캐시. 있으면 다시 받지 않는다. 받는 중 실패하면 .part만 남고 캐시로 쓰이지 않는다."""
    target = Path(target)
    if target.exists() and target.stat().st_size > 0:
        return Fetched(url, target, _sha256(target), target.stat().st_size, cached=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    result = downloader(url, part, config)
    part.replace(target)
    return Fetched(url, target, result.sha256, result.size)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
