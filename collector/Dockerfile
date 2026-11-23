# collector 매일 수집 이미지 (Kubernetes CronJob). 빌드 컨텍스트는 저장소 루트다.
#
#   docker build -f collector/Dockerfile --build-arg GIT_SHA=$(git rev-parse HEAD) -t gpu-collector:dev .
#
# - Airflow DAG가 준비되기 전까지 매일 수집을 CronJob으로 실행한다(airflow/Dockerfile은 그대로 둔다).
# - 비밀값은 넣지 않는다. 실행할 때 Kubernetes Secret에서 환경 변수로 주입한다.
FROM python:3.12-slim

ARG GIT_SHA=unknown

COPY collector/pyproject.toml /tmp/gpu-collector/pyproject.toml
COPY collector/src /tmp/gpu-collector/src
RUN pip install --no-cache-dir /tmp/gpu-collector && rm -rf /tmp/gpu-collector

COPY collector/scripts/daily.sh /usr/local/bin/gpu-collector-daily
RUN chmod 755 /usr/local/bin/gpu-collector-daily && useradd --uid 10001 --create-home collector

# COLLECTOR_BUILD: 실행 기록(collection_run.collector_version)에 빌드 Git SHA를 남긴다
ENV COLLECTOR_BUILD=${GIT_SHA} \
    CACHE_DIR=/tmp/gpu-collector-cache \
    PYTHONUNBUFFERED=1

USER 10001
WORKDIR /home/collector
ENTRYPOINT ["gpu-collector-daily"]
