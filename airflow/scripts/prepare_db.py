"""Airflow 메타 DB 준비. 이 다음에 `airflow db migrate`를 실행한다.

  python prepare_db.py                    AIRFLOW__DATABASE__SQL_ALCHEMY_SCHEMA 스키마가 없으면 만든다
  python prepare_db.py --create-database  (로컬 전용) 접속 문자열의 DB가 없으면 먼저 만든다

메타 테이블은 수집 데이터와 같은 DB여도 별도 스키마(airflow)에 둔다.
"""
import os
import re
import sys

import psycopg
from psycopg import sql


def libpq_url(url):
    """SQLAlchemy 접속 문자열(postgresql+psycopg2://)을 libpq 형식으로."""
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def create_database(url):
    info = psycopg.conninfo.conninfo_to_dict(url)
    name = info["dbname"]
    with psycopg.connect(psycopg.conninfo.make_conninfo(url, dbname="postgres"), autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            print(f"DB 생성: {name}")


def main(argv):
    url = libpq_url(os.environ["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"])
    schema = os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_SCHEMA")
    if "--create-database" in argv:
        create_database(url)
    if schema:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        print(f"스키마 준비: {schema}")


if __name__ == "__main__":
    main(sys.argv[1:])
