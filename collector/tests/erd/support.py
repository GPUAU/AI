"""ERD 테스트 공용 도구.

TEST_DATABASE_URL(관리용 DB 접속)로 서버에 붙어 임시 DB를 만들고, 테스트가 끝나면 지운다.
TEST_DATABASE_URL이 없으면 DB를 쓰는 테스트는 건너뛴다.
"""
import os
import unittest
import uuid
from importlib import resources

import psycopg
from psycopg.conninfo import make_conninfo

SQL = resources.files("gpu_collector") / "sql"
DDL = SQL / "ddl.sql"
SEED = SQL / "seed.sql"


def admin_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        raise unittest.SkipTest("TEST_DATABASE_URL이 없어 DB 테스트를 건너뜀")
    return url


def create_database():
    """임시 DB를 만들고 이름을 돌려준다."""
    name = f"erd_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin_url(), autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    return name


def connect(name, **kwargs):
    """임시 DB에 접속한다. 관리용 접속 정보에서 DB 이름만 바꾼다."""
    return psycopg.connect(make_conninfo(admin_url(), dbname=name), **kwargs)


def drop_database(name):
    with psycopg.connect(admin_url(), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
