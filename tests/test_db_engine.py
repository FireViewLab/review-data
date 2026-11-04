"""애플리케이션 DB 엔진의 연결 풀 설정을 검증한다."""

from review_data.core.db.base import create_engine
from review_data.core.settings import get_settings


def test_engine_recycles_and_checks_connections(monkeypatch):
    monkeypatch.setenv("DATABASE_POOL_RECYCLE_SECONDS", "600")
    get_settings.cache_clear()
    try:
        engine = create_engine()
        # 오래 쓴 연결은 DB 쪽 메모리가 쌓이므로 주기적으로 새로 맺고, 끊긴 연결은 쓰기 전에 거른다.
        assert engine.pool._recycle == 600
        assert engine.pool._pre_ping
    finally:
        get_settings.cache_clear()
