"""쿠팡 파트너스 검색 서명·파싱과 Akamai 차단 판별 (네트워크/DB 없음)."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from playwright.async_api import Error as PlaywrightError

from review_data.collectors.coupang.collector import (
    _BLOCKED_MARKER,
    PARTNERS_SEARCH_PATH,
    CoupangCollector,
    _is_blocked_page,
    build_partners_authorization,
    build_search_query,
    sign_partners_request,
)
from review_data.core.collection_failure import failure_kind
from review_data.core.discovery import discover
from review_data.core.exceptions import AccessBlockedError, MissingCredentialError, ParseError
from review_data.core.settings import Settings

AKAMAI_BODY = """<HTML><HEAD>
<TITLE>Access Denied</TITLE>
</HEAD><BODY>
<H1>Access Denied</H1>

You don't have permission to access
"http&#58;&#47;&#47;www&#46;coupang&#46;com&#47;vp&#47;products&#47;123"
on this server.<P>
Reference&#32;&#35;18&#46;5c3b2f17&#46;1760572800&#46;1a2b3c4d
<P>https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;18&#46;5c3b2f17&#46;1760572800&#46;1a2b3c4d</P>
</BODY>
</HTML>
"""

NORMAL_PRODUCT_HTML = """
<html><head><title>Access Denied 레터링 티셔츠 - 쿠팡!</title></head><body>
<h1 class="product-title">Access Denied 레터링 티셔츠</h1>
<p>You don't have permission to access 이 문구는 상품 설명입니다.</p>
<div class="total-price"><strong>12,900원</strong></div>
<img class="prod-image__detail" src="//thumbnail.coupangcdn.com/a.jpg">
<div class="prod-buy-header__info"><span class="rating-star-num">4.5</span>
<span class="count">1,234개 상품평</span></div>
</body></html>
"""


def _settings(**kwargs) -> Settings:
    values = {
        "coupang_partners_access_key": "test-access",
        "coupang_partners_secret_key": "test-secret",
        "request_delay": 0,
    }
    values.update(kwargs)
    return Settings(_env_file=None, **values)


# ── 서명 ─────────────────────────────────────


def test_search_query_encodes_korean_keyword():
    assert (
        build_search_query("선크림 50ml", 10)
        == "keyword=%EC%84%A0%ED%81%AC%EB%A6%BC%2050ml&limit=10"
    )


def test_signature_and_authorization_header_are_fixed():
    query = build_search_query("선크림 50ml", 10)

    assert (
        sign_partners_request("test-secret", "GET", PARTNERS_SEARCH_PATH, query, "261016T010203Z")
        == "04e21d1ad67390b96598c1c61212643795ad8c5783f58ca3984b52cc964c8b3b"
    )
    assert build_partners_authorization(
        "test-access",
        "test-secret",
        "GET",
        PARTNERS_SEARCH_PATH,
        query,
        datetime(2026, 10, 16, 1, 2, 3, tzinfo=UTC),
    ) == (
        "CEA algorithm=HmacSHA256, access-key=test-access, signed-date=261016T010203Z, "
        "signature=04e21d1ad67390b96598c1c61212643795ad8c5783f58ca3984b52cc964c8b3b"
    )


# ── 검색 ─────────────────────────────────────


def _search_collector(handler, **settings) -> tuple[CoupangCollector, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    collector = CoupangCollector(_settings(**settings))
    collector._client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return collector, requests


def _ok(products: list[dict]) -> httpx.Response:
    return httpx.Response(
        200, json={"rCode": "0", "rMessage": "", "data": {"productData": products}}
    )


async def test_search_converts_partners_products():
    collector, requests = _search_collector(
        lambda request: _ok(
            [
                {
                    "productId": 7334792436,
                    "productName": "선크림 50ml",
                    "productPrice": 15900,
                    "productImage": "https://thumbnail.coupangcdn.com/a.jpg",
                    "productUrl": "https://link.coupang.com/re/AFFSDP?pageKey=7334792436",
                    "categoryName": "스킨케어",
                    "isRocket": True,
                },
                {"productId": 2, "productName": "", "productPrice": 1000},
                {
                    "productId": 3,
                    "productName": "톤업 선크림",
                    "productPrice": 9900,
                    "productImage": "",
                    "productUrl": "https://link.coupang.com/x",
                    "categoryName": "",
                    "isRocket": False,
                },
            ]
        )
    )

    products = await collector.search_products("선크림", limit=20)

    assert [p.product_id for p in products] == ["7334792436", "3"]
    first, second = products
    assert first.url == "https://www.coupang.com/vp/products/7334792436"
    assert first.price == 15900
    assert first.category == "스킨케어"
    assert first.thumbnail_url == "https://thumbnail.coupangcdn.com/a.jpg"
    assert first.rating is None and first.review_count is None
    assert second.category is None and second.thumbnail_url is None

    request = requests[0]
    assert request.url.path == PARTNERS_SEARCH_PATH
    # limit 이 10을 넘으면 API 상한인 10으로 요청한다.
    assert parse_qs(request.url.query.decode())["limit"] == ["10"]
    query = request.url.query.decode()
    assert query == build_search_query("선크림", 10)
    authorization = request.headers["Authorization"]
    assert authorization.startswith("CEA algorithm=HmacSHA256, access-key=test-access, ")
    signed_date = authorization.split("signed-date=")[1].split(",")[0]
    expected = sign_partners_request(
        "test-secret", "GET", PARTNERS_SEARCH_PATH, query, signed_date
    )
    assert authorization.endswith(f"signature={expected}")


async def test_search_small_limit_is_sent_as_is():
    collector, requests = _search_collector(lambda request: _ok([]))

    assert await collector.search_products("텀블러", limit=3) == []
    assert parse_qs(requests[0].url.query.decode())["limit"] == ["3"]


async def test_search_rcode_error_is_parse_error():
    collector, _ = _search_collector(
        lambda request: httpx.Response(
            200, json={"rCode": "400", "rMessage": "limit is out of range", "data": None}
        )
    )

    with pytest.raises(ParseError, match="limit is out of range"):
        await collector.search_products("텀블러")


@pytest.mark.parametrize(
    "missing", ["coupang_partners_access_key", "coupang_partners_secret_key"]
)
async def test_search_without_keys_fails_before_request(missing):
    collector, requests = _search_collector(lambda request: _ok([]), **{missing: None})

    with pytest.raises(MissingCredentialError) as caught:
        await collector.search_products("텀블러")

    assert requests == []
    assert failure_kind(caught.value) == "credentials"


def test_collector_without_keys_can_be_created():
    # 키는 검색에만 필요하므로 생성 시점에 실패하지 않는다.
    CoupangCollector(Settings(_env_file=None))


@pytest.mark.parametrize("status", [403, 429])
async def test_search_blocked_status_is_classified_as_blocked(status):
    collector, _ = _search_collector(lambda request: httpx.Response(status, text="denied"))

    with pytest.raises(httpx.HTTPStatusError) as caught:
        await collector.search_products("텀블러")

    assert failure_kind(caught.value) == "blocked"


# ── 차단 판별 ─────────────────────────────────


@pytest.mark.parametrize(
    "html, blocked",
    [
        (AKAMAI_BODY, True),
        ("<title>Access Denied</title>", True),
        ("<h1>Access Denied</h1><p>errors.edgesuite.net/18.abc</p>", True),
        ("<h1>Access Denied</h1>", False),
        ("<title>Access Denied 앨범 - 쿠팡!</title>", False),
        (NORMAL_PRODUCT_HTML, False),
        ('<h1 class="product-title">Access Denied</h1><p>errors.edgesuite.net</p>', False),
    ],
)
def test_is_blocked_page(html, blocked):
    assert _is_blocked_page(html) is blocked


@pytest.fixture
def collector_page(monkeypatch):
    page = SimpleNamespace(
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        content=AsyncMock(return_value=NORMAL_PRODUCT_HTML),
        wait_for_selector=AsyncMock(),
        evaluate=AsyncMock(return_value=""),
    )
    closed = AsyncMock()

    @asynccontextmanager
    async def open_page():
        try:
            yield page
        finally:
            await closed()

    collector = CoupangCollector(Settings(_env_file=None))
    monkeypatch.setattr(collector, "page", open_page)
    monkeypatch.setattr(collector, "polite_wait", AsyncMock())
    return collector, page, closed


@pytest.mark.parametrize("method", ["get_product", "get_reviews"])
@pytest.mark.parametrize(
    "status, html, message",
    [
        (403, "", "HTTP 403"),
        (429, "", "HTTP 429"),
        (200, AKAMAI_BODY, "Access Denied"),
    ],
)
async def test_blocked_navigation_fails_before_selector_or_review_fetch(
    collector_page, method, status, html, message
):
    collector, page, closed = collector_page
    page.goto.return_value = SimpleNamespace(status=status)
    page.content.return_value = html

    with pytest.raises(AccessBlockedError, match=message) as caught:
        await getattr(collector, method)("123")

    assert failure_kind(caught.value) == "blocked"
    page.goto.assert_awaited_once()
    assert page.goto.await_args.args[0] == "https://www.coupang.com/vp/products/123"
    page.wait_for_selector.assert_not_awaited()
    page.evaluate.assert_not_awaited()
    closed.assert_awaited_once()
    if status != 200:
        page.content.assert_not_awaited()


async def test_other_http_error_is_parse_error_not_blocked(collector_page):
    collector, page, _ = collector_page
    page.goto.return_value = SimpleNamespace(status=500)

    with pytest.raises(ParseError) as caught:
        await collector.get_product("123")

    assert not isinstance(caught.value, AccessBlockedError)


async def test_normal_product_page_with_access_denied_name_is_parsed(collector_page):
    collector, page, closed = collector_page

    product = await collector.get_product("123")

    page.wait_for_selector.assert_awaited_once()
    assert product.name == "Access Denied 레터링 티셔츠"
    assert product.url == "https://www.coupang.com/vp/products/123"
    assert product.price == 12900
    assert product.thumbnail_url == "https://thumbnail.coupangcdn.com/a.jpg"
    assert product.rating == 4.5
    assert product.review_count == 1234
    closed.assert_awaited_once()


def _review_article(review_id: str, content: str) -> str:
    return f"""
    <article class="sdp-review__article__list">
      <span class="sdp-review__article__list__info__user__name">김**</span>
      <div class="sdp-review__article__list__info__product-info__star-orange" data-rating="5"></div>
      <div class="sdp-review__article__list__info__product-info__reg-date">2026.10.01</div>
      <div class="sdp-review__article__list__info__product-info__name">블랙, M</div>
      <div class="sdp-review__article__list__review__content">{content}</div>
      <div class="sdp-review__article__list__help" 
        data-review-id="{review_id}" data-count="3"></div>
    </article>
    """


async def test_reviews_paginate_skip_empty_content_and_stop_on_empty_page(collector_page):
    collector, page, _ = collector_page
    page.evaluate.side_effect = [
        _review_article("1", "좋아요") + _review_article("2", ""),
        _review_article("3", "Access Denied 문구가 프린트돼 있어요"),
        "",
    ]

    reviews = await collector.get_reviews("123", limit=50)

    assert [r.review_id for r in reviews] == ["1", "3"]
    assert reviews[0].rating == 5.0
    assert reviews[0].author == "김**"
    assert reviews[0].option == "블랙, M"
    assert reviews[0].helpful_count == 3
    assert reviews[0].written_at == datetime(2026, 10, 1)
    assert page.evaluate.await_count == 3
    assert page.evaluate.await_args_list[1].args[1]["page"] == 2
    assert collector.polite_wait.await_count == 2


async def test_reviews_are_cut_at_limit(collector_page):
    collector, page, _ = collector_page
    page.evaluate.return_value = "".join(_review_article(str(i), f"리뷰 {i}") for i in range(10))

    reviews = await collector.get_reviews("123", limit=3)

    assert len(reviews) == 3
    page.evaluate.assert_awaited_once()
    collector.polite_wait.assert_not_awaited()


async def test_review_fetch_blocked_status_is_access_blocked(collector_page):
    collector, page, _ = collector_page
    page.evaluate.side_effect = PlaywrightError(f"Error: {_BLOCKED_MARKER}:403")

    with pytest.raises(AccessBlockedError):
        await collector.get_reviews("123")


async def test_review_fetch_akamai_body_is_access_blocked(collector_page):
    collector, page, _ = collector_page
    page.evaluate.return_value = AKAMAI_BODY

    with pytest.raises(AccessBlockedError, match="Access Denied"):
        await collector.get_reviews("123")

    collector.polite_wait.assert_not_awaited()


# ── 자동 등록 ─────────────────────────────────


def test_coupang_is_discovered():
    registry, failures = discover()

    assert not failures
    assert registry["coupang"] is CoupangCollector
    assert CoupangCollector.label == "쿠팡"


def test_akamai_body_without_title_is_detected_from_entity_encoded_text():
    assert _is_blocked_page(AKAMAI_BODY.replace("<TITLE>Access Denied</TITLE>", ""))
