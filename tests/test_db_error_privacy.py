"""DB 오류 문자열에 SQL 파라미터(리뷰 본문)가 실리지 않는다."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from review_data.core.db import base

from .conftest import DATABASE_URL


async def test_db_errors_hide_parameters(monkeypatch):
    settings = base.get_settings().model_copy(update={"database_url": DATABASE_URL})
    monkeypatch.setattr(base, "get_settings", lambda: settings)
    engine = base.create_engine()
    secret = "리뷰 본문 비공개 문장"
    try:
        async with engine.connect() as conn:
            with pytest.raises(DBAPIError) as caught:
                # 오류 메시지 자체에는 값이 들어가지 않는 실패(0으로 나누기)로 파라미터만 본다.
                await conn.execute(text("SELECT CAST(:value AS text), 1 / 0"), {"value": secret})
    finally:
        await engine.dispose()
    assert secret not in str(caught.value)
    assert "hide_parameters" in str(caught.value)
