FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TEMPLATE_DIR=/data/templates
# 네트워크가 느린 러너에서도 PyPI 다운로드가 끊기지 않게: 시간 제한·재시도를 늘린다.
# 환경변수로 두는 이유: pip이 빌드 의존성을 설치하는 하위 프로세스에도 그대로 적용된다 (명령줄 옵션은 넘어가지 않음).
ENV PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=5 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir ".[ocr]" && useradd -u 10001 -m agent && mkdir -p /data/templates && chown -R agent /data
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
