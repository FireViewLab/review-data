# API 와 워커가 같은 이미지를 쓴다. 실행 명령만 docker-compose.yml 에서 다르게 준다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 비루트 사용자도 읽을 수 있도록 브라우저를 홈이 아닌 고정 경로에 설치한다.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 일부 쇼핑몰(오늘의집)은 headless 브라우저를 막아서 화면 모드로 띄운다. 서버에는 화면이 없으므로
# 가상 화면(Xvfb)을 함께 설치한다.
#
# 의존성과 Chromium 은 소스보다 훨씬 드물게 바뀐다. 먼저 설치해 두면 코드만 고친
# 빌드에서는 이 무거운 레이어(수백 MB)를 캐시에서 재사용한다.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > requirements.txt \
    && pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && apt-get update && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY seeds.toml ./seeds.toml
COPY src ./src
RUN pip install --no-deps .

COPY alembic.ini ./
COPY alembic ./alembic

RUN useradd --create-home app
USER app

EXPOSE 8000
CMD ["uvicorn", "review_data.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
