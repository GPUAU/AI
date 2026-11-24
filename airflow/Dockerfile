# Airflow + collector 이미지. 빌드 컨텍스트는 저장소 루트다.
#
#   docker build -f airflow/Dockerfile --build-arg GIT_SHA=$(git rev-parse HEAD) -t gpu-collector-airflow:dev .
#
# - slim 이미지에 필요한 provider만 넣는다: slack(알림), amazon(S3 로그), postgres(메타 DB 드라이버 psycopg2).
# - collector는 Airflow constraints로 설치해 Airflow가 검증한 의존성 버전에 맞춘다(collector는 호환 범위만 적는다).
# - DAG와 collector를 한 이미지로 배포한다. 비밀값(.env·키)은 넣지 않고 실행할 때 환경 변수로 주입한다.
ARG AIRFLOW_VERSION=3.3.2
ARG PYTHON_VERSION=3.12
FROM apache/airflow:slim-${AIRFLOW_VERSION}-python${PYTHON_VERSION}

ARG AIRFLOW_VERSION
ARG PYTHON_VERSION
ARG GIT_SHA=unknown

COPY --chown=airflow:root collector/pyproject.toml /tmp/gpu-collector/pyproject.toml
COPY --chown=airflow:root collector/src /tmp/gpu-collector/src

RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" \
        apache-airflow-providers-slack apache-airflow-providers-amazon apache-airflow-providers-postgres \
        /tmp/gpu-collector \
        --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt" \
    && pip check \
    && rm -rf /tmp/gpu-collector

COPY --chown=airflow:root airflow/dags /opt/airflow/dags
COPY --chown=airflow:root airflow/scripts /opt/airflow/scripts

# COLLECTOR_BUILD: 실행 기록(collection_run.collector_version)에 빌드 Git SHA를 남긴다
# CACHE_DIR: AWS 과거 가격 파일 등 원천 파일 캐시(컨테이너 임시 공간)
ENV COLLECTOR_BUILD=${GIT_SHA} \
    CACHE_DIR=/tmp/gpu-collector-cache \
    AIRFLOW__CORE__LOAD_EXAMPLES=False \
    AIRFLOW__CORE__DEFAULT_TIMEZONE=Asia/Seoul \
    AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION=True
