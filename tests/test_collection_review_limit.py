"""HTTP·브라우저 수집기가 실제로 서로 다른 설정 상한을 전달받는지 검증한다."""

import pytest

from review_data.core.base import BaseCollector
from review_data.core.browser import BrowserCollector
from review_data.core.models import Product
from review_data.core.settings import Settings
from review_data.worker.collection_worker import _Claim, _collect


class HttpCollector(BaseCollector):
    platform = "test"
    observed = None

    async def search_products(self, keyword, limit=20):
        return []

    async def get_product(self, product_id):
        return Product(platform=self.platform, product_id=product_id, name="상품", url="https://x")

    async def get_reviews(self, product_id, limit=50):
        type(self).observed = limit
        return []


class BrowserTestCollector(HttpCollector, BrowserCollector):
    pass


@pytest.mark.parametrize("collector,expected", [(HttpCollector, 250), (BrowserTestCollector, 75)])
async def test_worker_passes_configured_review_limit(collector, expected):
    config = Settings(_env_file=None, review_collect_limit=250, browser_review_collect_limit=75)
    result = await _collect(collector, _Claim(1, "test", "p", "worker"), config)
    assert not result.errors
    assert collector.observed == expected


@pytest.mark.parametrize("key", ["review_collect_limit", "browser_review_collect_limit"])
@pytest.mark.parametrize("limit", [0, 1001])
def test_unbounded_collection_is_rejected(key, limit):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{key: limit})
