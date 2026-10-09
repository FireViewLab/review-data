"""실제 HTTP 검색부터 저장·수집 예약과 인증된 카탈로그 조회까지 검증한다."""

import importlib

from fastapi.testclient import TestClient

from review_data.api import v1
from review_data.core.base import BaseCollector
from review_data.core.db.repository import ReviewRepository
from review_data.core.models import Product, Review
from review_data.core.settings import Settings, get_settings

module = importlib.import_module("review_data.api.app")


class Collector(BaseCollector):
    platform = "catalogplat"

    async def setup(self):
        pass

    async def search_products(self, keyword, limit=20):
        return [Product(platform=self.platform, product_id="p", name="검색 상품", url="https://x")]

    async def get_product(self, product_id):
        raise NotImplementedError

    async def get_reviews(self, product_id, limit=50):
        raise NotImplementedError


def test_search_registers_and_catalog_auth_cursor_are_enforced(engine, monkeypatch):
    settings = Settings(
        _env_file=None,
        database_url=get_settings().database_url,
        internal_token="test-token",
        ai_analysis_enabled=True,
        ai_stream_url="http://test.invalid/stream",
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(v1, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "_get_collector_cls", lambda platform: Collector)
    headers = {"X-Internal-Token": "test-token"}
    with TestClient(module.app) as client:
        assert client.get("/api/v1/catalog").status_code == 401
        response = client.get(
            "/catalogplat/search", params={"keyword": "상품", "limit": 1}, headers=headers
        )
        assert response.status_code == 200 and response.json() == []
        detail = client.get("/api/v1/catalogplat/products/p", headers=headers).json()
        assert detail["job"]["status"] == "pending"
        assert (
            client.get(
                "/catalogplat/search", params={"keyword": "상품"}, headers=headers
            ).status_code
            == 200
        )
        repeat = client.get("/api/v1/catalogplat/products/p", headers=headers).json()
        assert repeat["job"]["id"] == detail["job"]["id"]
        page = client.get("/api/v1/catalog", headers=headers).json()
        # 검색 등록은 수집을 예약하지만, 실제 리뷰가 없는 상품은 목록에 내리지 않는다.
        assert page["items"] == [] and page["next_cursor"] is None

        async def save_review():
            async with module.app.state.session_factory() as session:
                await ReviewRepository(session).upsert_many(
                    "catalogplat",
                    "p",
                    [Review(platform="catalogplat", product_id="p", review_id="r", content="리뷰")],
                )
                await session.commit()

        client.portal.call(save_review)
        ready = client.get("/catalogplat/search", params={"keyword": "상품"}, headers=headers)
        assert len(ready.json()) == 1
        page = client.get("/api/v1/catalog", headers=headers).json()
        assert page["items"][0]["product"]["product_id"] == "p"
        assert page["items"][0]["analysis"]["status"] == "not_analyzed"
        assert (
            client.get("/api/v1/catalog", params={"cursor": "bad"}, headers=headers).status_code
            == 400
        )
        assert (
            client.get("/api/v1/catalog", params={"limit": 101}, headers=headers).status_code == 422
        )
        assert (
            client.get(
                "/catalogplat/search", params={"keyword": "상품", "limit": 101}, headers=headers
            ).status_code
            == 422
        )
