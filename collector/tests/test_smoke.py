"""개발 환경 확인용 기본 테스트."""
import os

import psycopg
import pytest

import gpu_collector


def test_package_imports():
    assert gpu_collector.__version__


def test_database_reachable():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL이 없어 DB 접속 확인을 건너뜀")
    with psycopg.connect(url, connect_timeout=5) as conn:
        version = conn.execute("SHOW server_version_num").fetchone()[0]
    assert int(version) >= 170000, "PostgreSQL 17 이상이어야 한다"
