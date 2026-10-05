"""
설정 로딩.

⚠️ 중요: import 시점에 Settings 를 만들지 않습니다.

이 패키지는 나중에 본 서버(FastAPI)에 라이브러리로 이식됩니다.
import 만으로 .env 를 강제하거나 종료해버리면 호스트 앱이 죽으므로,
실제로 값이 필요한 시점(get_settings 호출)에 lazy 로 생성합니다.

- CLI 로 쓸 때  : cli.py 가 .env 존재 여부를 검사하고 친절히 안내
- 라이브러리로 쓸 때 : OS 환경변수/호스트 설정을 그대로 사용, .env 없어도 무관
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/review_data/core/settings.py -> 프로젝트 루트
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 공통 ──────────────────────────────
    request_timeout: float = 15.0
    request_delay: float = 1.0
    headless: bool = True

    # PostgreSQL 접속 정보 (환경변수 DATABASE_URL 로 덮어쓸 수 있음)
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/review_data"

    # Spring·AI 등 내부 서버만 API를 호출하도록 공유하는 토큰. 로컬 개발에서는
    # 값을 비워 기존처럼 인증 없이 쓸 수 있다.
    internal_token: str | None = None

    # 신선도 기준 시간(초). 상품 정보(가격/평점)보다 리뷰가 더 자주 바뀌므로 따로 둔다.
    # 둘 중 하나라도 오래됐으면 재수집 job을 만든다.
    product_ttl_seconds: int = 24 * 60 * 60
    review_ttl_seconds: int = 6 * 60 * 60

    # 한 플랫폼에서 동시에 running 상태일 수 있는 job 수. 워커 프로세스 하나는 job 을
    # 순차 처리하므로, 이 값은 워커를 여러 개 띄웠을 때의 상한이다.
    # 브라우저 기반 collector 는 수집마다 Chromium 을 띄우므로 훨씬 낮게 잡는다.
    max_concurrent_jobs_per_platform: int = 4
    max_concurrent_browser_jobs_per_platform: int = 1

    # 수집 한 건에 허용할 시간(초). 넘기면 job 을 실패로 남기고 다음 job 으로 넘어간다.
    # 브라우저는 페이지 렌더링까지 기다려야 해서 더 넉넉히 준다.
    collect_timeout_seconds: float = 120.0
    browser_collect_timeout_seconds: float = 300.0

    # ── 스케줄러 ───────────────────────────
    # 낡은 상품을 찾아 수집 job 을 예약하는 주기(초).
    # 0 이하면 쉬지 않고 DB 를 조회하게 되므로 막는다.
    schedule_interval_seconds: float = Field(default=300.0, gt=0)
    # 대기 중인 job 이 이만큼 쌓여 있으면 스케줄러는 더 예약하지 않는다. 워커가 소화하는
    # 속도보다 빨리 쌓이면 큐만 길어지고, 조회로 들어온 급한 job 이 뒤로 밀린다.
    # 조회 API 가 만드는 job 은 이 상한과 무관하게 항상 만들어진다. 0 이면 예약을 멈춘다.
    schedule_max_pending: int = Field(default=20, ge=0)
    # 수집이 실패한 상품을 다시 예약하기까지 기다리는 시간(초). 차단된 플랫폼을
    # 주기마다 다시 두드리지 않기 위한 것이다.
    schedule_failure_cooldown_seconds: int = Field(default=6 * 60 * 60, ge=0)
    # 스케줄러가 건드리지 않을 플랫폼(쉼표 구분). 플랫폼 전체가 차단돼 있으면 상품별
    # 대기만으로는 상품 수만큼 요청이 반복되므로 여기서 통째로 뺀다. 예: "gmarket,auction"
    schedule_excluded_platforms: str = ""

    # ── 플랫폼별 인증 정보 ──────────────────
    # 자기 플랫폼에 키가 필요하면 여기에 추가하고,
    # .env.example 에도 반드시 빈 값으로 추가하세요.
    elevenst_api_key: str | None = None
    naver_client_id: str | None = None
    naver_client_secret: str | None = None

    def excluded_platforms(self) -> tuple[str, ...]:
        """예약과 시드가 같은 제외 목록을 써 차단 플랫폼을 다시 넣지 않게 한다."""
        return tuple(
            name.strip() for name in self.schedule_excluded_platforms.split(",") if name.strip()
        )


@lru_cache
def get_settings() -> Settings:
    """설정 싱글턴. 최초 호출 시에만 생성됩니다."""
    return Settings()


def env_file_exists() -> bool:
    """CLI 에서 .env 존재 여부를 안내하기 위한 헬퍼."""
    return ENV_FILE.exists()
