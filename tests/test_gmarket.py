"""G마켓 collector 단위 테스트(네트워크 없이 응답 파싱만 검증)."""

from datetime import datetime

from bs4 import BeautifulSoup

from review_data.collectors.gmarket.collector import (
    GmarketCollector,
    _extract_product_ld_json,
    _has_empty_marker,
    _is_blocked,
)
from review_data.core.discovery import discover

SEARCH_CARD = """
<div class="box__component">
  <div class="box__item-container">
    <div class="box__image"><img src="//gdimg.gmarket.co.kr/555/still/300" /></div>
    <div class="box__information">
      <div class="box__item-title">
        <span><a href="https://item.gmarket.co.kr/Item?goodsCode=555">
          <span class="text__item">샘플 텀블러</span>
        </a></span>
      </div>
      <div class="box__item-price"><div class="box__price-seller">
        <strong>12,900원</strong>
      </div></div>
      <div class="box__seller"><span class="text">판매자A</span></div>
      <span class="text__brand">브랜드A</span>
      <ul>
        <li class="list-item__feedback-count"><span class="text">상품평 1,234</span></li>
        <li class="list-item__score"><span class="for-a11y">평점 4.7</span></li>
      </ul>
    </div>
  </div>
</div>
"""

DETAIL_HTML = """
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org", "@type": "Product",
  "name": "JSON-LD 텀블러", "image": ["https://gdimg.gmarket.co.kr/555/main.jpg"],
  "brand": {"@type": "Brand", "name": "브랜드B"},
  "seller": {"@type": "Organization", "name": "판매자B"},
  "category": "주방용품 > 텀블러",
  "offers": {"@type": "Offer", "price": "10900"},
  "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.6", "reviewCount": "10503"}
}
</script>
</head><body></body></html>
"""

REVIEW_HTML = """
<div id="text-pagenation-wrap" data-total-page="6"></div>
<table class="tb_comment"><tbody>
  <tr>
    <td class="comment-content">
      <p class="comment-tit">늘</p>
      <p class="pd-tit">01_블랙</p>
      <p class="con">늘 쓰던 제품이에요</p>
    </td>
    <td class="info"><dl class="writer-info">
      <dt>작성자 :</dt><dd>jsy****</dd><dt>등록일 :</dt><dd>2026.08.31</dd>
    </dl></td>
  </tr>
  <tr>
    <td class="comment-content"><p class="pd-tit">옵션없음</p><p class="con">굿</p></td>
    <td class="info"><dl class="writer-info">
      <dt>작성자 :</dt><dd>dms*******</dd><dt>등록일 :</dt><dd>2026.08.30</dd>
    </dl></td>
  </tr>
  <tr><td class="other">상품평이 아닌 행</td></tr>
</tbody></table>
"""


def _collector() -> GmarketCollector:
    return GmarketCollector()


def test_discovery_registers_gmarket_collector():
    registry, failures = discover()
    assert not failures
    assert registry["gmarket"] is GmarketCollector


def test_parse_search_card_extracts_product_fields():
    card = BeautifulSoup(SEARCH_CARD, "lxml").select_one(".box__component")
    product = _collector()._parse_search_card(card)

    assert product is not None
    assert product.product_id == "555"
    assert product.name == "샘플 텀블러"
    assert product.url == "https://item.gmarket.co.kr/Item?goodsCode=555"
    assert product.brand == "브랜드A"
    assert product.seller == "판매자A"
    assert product.price == 12900
    assert product.thumbnail_url == "https://gdimg.gmarket.co.kr/555/still/300"
    assert product.review_count == 1234
    assert product.rating == 4.7


def test_parse_search_card_skips_card_without_goods_code():
    card = BeautifulSoup('<div><a href="https://example.com">상품</a></div>', "lxml").div
    assert _collector()._parse_search_card(card) is None


def test_extract_and_parse_product_ld_json():
    data = _extract_product_ld_json(DETAIL_HTML)
    assert data["name"] == "JSON-LD 텀블러"

    product = _collector()._parse_product_page("555", DETAIL_HTML)
    assert product.name == "JSON-LD 텀블러"
    assert product.brand == "브랜드B"
    assert product.seller == "판매자B"
    assert product.price == 10900
    assert product.category == "주방용품 > 텀블러"
    assert product.review_count == 10503
    assert product.rating == 4.6


def test_parse_review_page_extracts_rows_and_stable_ids():
    collector = _collector()
    reviews = collector._parse_review_page("555", REVIEW_HTML)
    same_reviews = collector._parse_review_page("555", REVIEW_HTML)

    assert len(reviews) == 2
    assert reviews[0].content == "늘 쓰던 제품이에요"
    assert reviews[0].author == "jsy****"
    assert reviews[0].written_at == datetime(2026, 8, 31)
    assert reviews[0].option == "01_블랙"
    assert reviews[0].rating is None
    assert reviews[0].review_id == same_reviews[0].review_id
    assert reviews[0].review_id.startswith("gm-")
    assert reviews[1].content == "굿"


def test_parse_total_pages_and_review_body():
    collector = _collector()
    assert collector._parse_total_pages(REVIEW_HTML) == 6
    assert collector._parse_total_pages("<html></html>") is None
    assert collector._review_body("4814731104", 1) == "goodsCode=4814731104&pageNo=1"
    assert collector._review_body("4814731104", 2, 6) == "goodsCode=4814731104&pageNo=2&totalPage=6"


def test_cloudflare_response_is_detected():
    assert _is_blocked(403, "")
    assert _is_blocked(200, "<title>잠시만 기다리십시오…</title>")
    assert not _is_blocked(200, DETAIL_HTML)


def test_empty_state_requires_explicit_marker():
    markers = ("검색결과가 없습니다",)
    assert _has_empty_marker("<p>검색결과가 없습니다.</p>", markers)
    assert not _has_empty_marker("<html><body></body></html>", markers)


async def test_empty_inputs_do_not_start_browser():
    collector = _collector()
    assert await collector.search_products("", limit=10) == []
    assert await collector.search_products("텀블러", limit=0) == []
    assert await collector.get_reviews("555", limit=0) == []


async def test_http_errors_are_reported_before_selector():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import pytest

    from review_data.core.collection_failure import failure_kind
    from review_data.core.exceptions import ParseError

    collector = _collector()
    for status, kind in [(403, "blocked"), (429, "blocked"), (404, "not_found"), (503, "network")]:
        page = SimpleNamespace(
            goto=AsyncMock(return_value=SimpleNamespace(status=status)),
            content=AsyncMock(return_value=""),
            wait_for_selector=AsyncMock(),
        )
        with pytest.raises(ParseError) as caught:
            await collector._load_page(page, "https://example.com", "h1")
        assert failure_kind(caught.value) == kind
        page.wait_for_selector.assert_not_awaited()


async def test_review_transport_error_does_not_claim_confirmed_block():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import pytest
    from playwright.async_api import Error

    from review_data.core.collection_failure import failure_kind
    from review_data.core.exceptions import ParseError

    page = SimpleNamespace(evaluate=AsyncMock(side_effect=Error("failed to fetch")))
    with pytest.raises(ParseError) as caught:
        await _collector()._fetch_review_html(page, "/review", "")
    assert failure_kind(caught.value) == "parse"
    assert "Cloudflare" not in str(caught.value)
