"""올리브영 collector 단위 테스트 (네트워크 없이 파싱/변환 로직만 검증)."""

from datetime import datetime

import pytest

from review_data.collectors.oliveyoung.collector import (
    OliveyoungCollector,
    _parse_date,
)

SEARCH_ITEM = {
    "goodsNumber": "A000000223414",
    "goodsName": "메디힐 에센셜 마스크팩",
    "priceToPay": 10000,
    "minimumPriceToPay": 10000,
    "onlineBrandName": "메디힐",
    "imagePath": "10/0000/0022/A000000223414117ko.jpg?l=ko",
    "middleCategoryName": "마스크팩",
    "goodsAssessmentTotalCount": 465003,
    "goodsEvaluationScoreValue": 4.9,
}

REVIEW_ITEM = {
    "reviewId": 63039348,
    "content": "진정 효과가 좋아요",
    "reviewScore": 5,
    "createdDateTime": "2026.07.20",
    "recommendCount": 23,
    "profileDto": {"memberNickname": "몽캔디"},
    "goodsDto": {"optionName": "마데카소사이드 흔적리페어 1매"},
    "photoReviewList": [
        {"imageSequence": 1, "imagePath": "2026/07/20/aaa.png"},
        {"imageSequence": 2, "imagePath": "2026/07/20/bbb.png"},
    ],
}


def _collector() -> OliveyoungCollector:
    return OliveyoungCollector()


def test_parse_date():
    assert _parse_date("2026.07.20") == datetime(2026, 7, 20)
    assert _parse_date("") is None
    assert _parse_date(None) is None


def test_parse_search_item():
    product = _collector()._parse_search_item(SEARCH_ITEM)
    assert product.product_id == "A000000223414"
    assert product.name == "메디힐 에센셜 마스크팩"
    assert product.price == 10000
    assert product.brand == "메디힐"
    assert product.category == "마스크팩"
    assert product.review_count == 465003
    assert product.rating == 4.9
    assert product.url == "https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo=A000000223414"
    assert product.thumbnail_url.endswith("10/0000/0022/A000000223414117ko.jpg?l=ko")


def test_parse_search_item_skips_incomplete():
    assert _collector()._parse_search_item({"goodsName": "이름만"}) is None
    assert _collector()._parse_search_item({"goodsNumber": "A1"}) is None


def test_parse_review_extracts_all_fields():
    review = _collector()._parse_review("A000000223414", REVIEW_ITEM)
    assert review.review_id == "63039348"
    assert review.content == "진정 효과가 좋아요"
    assert review.rating == 5.0
    assert review.author == "몽캔디"
    assert review.written_at == datetime(2026, 7, 20)
    assert review.option == "마데카소사이드 흔적리페어 1매"
    assert review.helpful_count == 23
    assert review.images == [
        "https://image.oliveyoung.co.kr/uploads/images/gdasEditor/2026/07/20/aaa.png",
        "https://image.oliveyoung.co.kr/uploads/images/gdasEditor/2026/07/20/bbb.png",
    ]


def test_parse_review_without_photos():
    item = {**REVIEW_ITEM, "photoReviewList": None}
    review = _collector()._parse_review("A1", item)
    assert review.images == []


async def test_search_empty_keyword_returns_empty():
    assert await _collector().search_products("", limit=10) == []


@pytest.mark.parametrize("content", [None, "", "   "])
def test_parse_review_without_content_is_skipped(content):
    # 별점만 남긴 리뷰 하나 때문에 상품의 리뷰 수집 전체가 실패하면 안 된다.
    assert _collector()._parse_review("A1", {**REVIEW_ITEM, "content": content}) is None


async def test_reviews_limit_zero_returns_empty():
    assert await _collector().get_reviews("A1", limit=0) == []
