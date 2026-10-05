"""네이버 브랜드스토어 collector 단위 테스트(네트워크 없이 응답 변환만 검증)."""

import json
from pathlib import Path

import pytest

from review_data.collectors.naver.collector import (
    NaverCollector,
    is_blocked,
    parse_product,
    parse_product_id,
    parse_related,
    parse_reviews,
)
from review_data.core.discovery import discover
from review_data.core.exceptions import NotSupportedError, ParseError

FIXTURES = Path(__file__).parent / "fixtures" / "naver"
PRODUCT_ID = "zinus:6000252751"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_registered_in_discovery():
    registry, _ = discover()
    assert registry["naver"] is NaverCollector


@pytest.mark.parametrize(
    ("value", "expected"),
    [("zinus:6000252751", ("zinus", "6000252751")), (" my-store_1:42 ", ("my-store_1", "42"))],
)
def test_parse_product_id(value, expected):
    assert parse_product_id(value) == expected


def test_product_url_is_rejected_with_correct_id_hint():
    with pytest.raises(ParseError, match="'zinus:6000252751'"):
        parse_product_id("https://brand.naver.com/zinus/products/6000252751?NaPm=x")


def test_smartstore_is_not_supported():
    with pytest.raises(NotSupportedError):
        parse_product_id("https://smartstore.naver.com/pasteur365/products/11150965069")


@pytest.mark.parametrize("value", ["6000252751", "zinus/6000252751", "zinus:abc", ""])
def test_invalid_product_id(value):
    with pytest.raises(ParseError):
        parse_product_id(value)


def test_parse_product_maps_standard_fields():
    product = parse_product(_load("product.json"), PRODUCT_ID)

    assert product.platform == "naver"
    assert product.product_id == PRODUCT_ID
    assert product.name == "지누스 1500H 침대 프레임 (슈퍼싱글)"
    assert product.url == "https://brand.naver.com/zinus/products/6000252751"
    # 할인가가 있으면 실제로 결제하는 금액을 쓴다.
    assert product.price == 152000
    assert product.brand == "지누스"
    assert product.seller == "지누스몰"
    assert product.category == "가구/인테리어>침실가구>침대>침대프레임"
    assert product.review_count == 1091
    assert product.rating == 4.65
    assert product.thumbnail_url.startswith("https://")


def test_parse_product_falls_back_to_sale_price():
    data = _load("product.json")
    data["benefitsView"] = {}
    assert parse_product(data, PRODUCT_ID).price == 190000


def test_parse_product_without_name_fails():
    data = _load("product.json")
    data["name"] = ""
    with pytest.raises(ParseError):
        parse_product(data, PRODUCT_ID)


def test_parse_reviews_maps_fields_and_keeps_only_images():
    reviews = parse_reviews(_load("reviews.json"), PRODUCT_ID, limit=20)

    assert len(reviews) == 3
    first = reviews[0]
    assert first.platform == "naver"
    assert first.product_id == PRODUCT_ID
    assert first.review_id == "5049864928"
    assert first.content == "조립이 쉬웠고 소음이 없어요."
    assert first.rating == 5.0
    assert first.written_at is not None and first.written_at.tzinfo is not None
    assert len(first.images) == 2
    # 영상 첨부(M)는 이미지 목록에 넣지 않는다.
    assert reviews[1].images == []


def test_parse_reviews_does_not_store_personal_fields():
    for review in parse_reviews(_load("reviews.json"), PRODUCT_ID, limit=20):
        dumped = review.model_dump_json()
        assert review.author is None
        for leaked in ("test****", "fake-writer", "0000000000"):
            assert leaked not in dumped


def test_parse_reviews_respects_limit_and_skips_empty_content():
    data = _load("reviews.json")
    data["contents"][0]["reviewContent"] = "   "

    reviews = parse_reviews(data, PRODUCT_ID, limit=1)

    assert [r.review_id for r in reviews] == ["5071490604"]


def test_parse_reviews_rejects_unexpected_shape():
    with pytest.raises(ParseError):
        parse_reviews({"contents": None}, PRODUCT_ID, limit=20)


@pytest.mark.parametrize(
    ("status", "html", "url", "blocked"),
    [
        (200, "<html>상품</html>", "https://brand.naver.com/zinus/products/1", False),
        # 일반 페이지에도 로그인 버튼 링크(nidlogin)는 있으므로 본문만으로 판단하지 않는다.
        (
            200,
            '<a href="https://nid.naver.com/nidlogin.login">로그인</a>',
            "https://brand.naver.com/x",
            False,
        ),
        (200, "", "https://nid.naver.com/nidlogin.login?url=x", True),
        (490, "", "https://smartstore.naver.com/x", True),
        (429, "", "https://brand.naver.com/x", True),
        (418, "", "https://search.shopping.naver.com/x", True),
        (200, "<title>보안 확인</title>", "https://brand.naver.com/x", True),
    ],
)
def test_is_blocked(status, html, url, blocked):
    assert is_blocked(status, html, url) is blocked


async def test_search_is_not_supported():
    with pytest.raises(NotSupportedError):
        await NaverCollector().search_products("텀블러")


def test_headless_override_does_not_touch_given_settings():
    from review_data.core.settings import Settings

    given = Settings(headless=True)
    collector = NaverCollector(settings=given)

    assert collector.settings.headless is False
    assert given.headless is True


def test_parse_related_keeps_only_same_store_products_on_sale():
    data = _load("related.json")

    related = parse_related(
        [data["simple_products"], data["other_recommend"]],
        channel_uid="2sWDwlB4wqRisSBxpyXQW",
        store="zinus",
        exclude_product_no="6000252751",
        limit=20,
    )

    # 방문한 상품 자신, 다른 스토어 상품, 판매 중지 상품, 중복은 빠진다.
    assert related == [
        "zinus:9524657101",
        "zinus:11887628580",
        "zinus:8435643749",
        "zinus:3744162440",
        "zinus:6000123816",
        "zinus:3744166541",
    ]


def test_parse_related_respects_limit():
    data = _load("related.json")

    related = parse_related(
        [data["simple_products"]],
        channel_uid="2sWDwlB4wqRisSBxpyXQW",
        store="zinus",
        exclude_product_no="6000252751",
        limit=2,
    )

    assert related == ["zinus:9524657101", "zinus:11887628580"]
