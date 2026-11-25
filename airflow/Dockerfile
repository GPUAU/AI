# Airflow + collector 이미지. 빌드 컨텍스트는 저장소 루트다.
#
#   docker build -f airflow/Dockerfile --build-arg GIT_SHA=$(git rev-parse HEAD) -t gpu-collector-airflow:dev .
#
# - slim 이미지에 필요한 provider만 넣는다: slack(알림), amazon(S3 로그), postgres(메타 DB 드라이버 psycopg2).
# - collector 의존성은 Airflow constraints로 설치해 Airflow가 검증한 버전에 맞춘다(collector는 호환 범위만 적는다).
# - DAG와 collector를 한 이미지로 배포한다. 비밀값(.env·키)은 넣지 않고 실행할 때 환경 변수로 주입한다.
# - 레이어는 잘 안 바뀌는 순서로 쌓는다: 의존성(pyproject.toml만 보고 설치) → collector 소스 → DAG·스크립트 → Git SHA.
#   collector 코드나 DAG만 바뀌면 의존성 레이어는 캐시를 쓰고, 레지스트리에도 다시 올라가지 않는다.
ARG AIRFLOW_VERSION=3.3.2
ARG PYTHON_VERSION=3.12
FROM apache/airflow:slim-${AIRFLOW_VERSION}-python${PYTHON_VERSION}

ARG AIRFLOW_VERSION
ARG PYTHON_VERSION

# 1) 의존성: Airflow·provider + collector가 쓰는 라이브러리(pyproject.toml의 dependencies)
COPY --chown=airflow:root collector/pyproject.toml /tmp/gpu-collector/pyproject.toml
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('/tmp/gpu-collector/pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/collector-requirements.txt \
    && pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" \
        apache-airflow-providers-slack apache-airflow-providers-amazon apache-airflow-providers-postgres \
        -r /tmp/collector-requirements.txt \
        --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt" \
    && rm /tmp/collector-requirements.txt

# 2) collector 패키지(의존성은 위에서 설치, 여기서는 소스만)
COPY --chown=airflow:root collector/src /tmp/gpu-collector/src
RUN pip install --no-cache-dir --no-deps /tmp/gpu-collector \
    && pip check \
    && rm -rf /tmp/gpu-collector

# 3) DAG·스크립트
COPY --chown=airflow:root airflow/dags /opt/airflow/dags
COPY --chown=airflow:root airflow/scripts /opt/airflow/scripts

# 4) 빌드 Git SHA는 커밋마다 바뀌어 맨 끝에 둔다(앞에 두면 그 뒤 RUN 캐시가 매번 깨진다)
# COLLECTOR_BUILD: 실행 기록(collection_run.collector_version)에 빌드 Git SHA를 남긴다
# CACHE_DIR: AWS 과거 가격 파일 등 원천 파일 캐시(컨테이너 임시 공간)
ARG GIT_SHA=unknown
ENV COLLECTOR_BUILD=${GIT_SHA} \
    CACHE_DIR=/tmp/gpu-collector-cache \
    AIRFLOW__CORE__LOAD_EXAMPLES=False \
    AIRFLOW__CORE__DEFAULT_TIMEZONE=Asia/Seoul \
    AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION=True
