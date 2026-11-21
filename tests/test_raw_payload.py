"""리뷰 원본 저장: 식별 정보 제거, API 비노출, 재수집 시 보존."""

from sqlalchemy import select, update

from review_data.core.db.models import ProductRow, ReviewRow
from review_data.core.db.repository import ProductRepository, ReviewRepository
from review_data.core.models import Product, Review
from review_data.core.raw_payload import MAX_BYTES, sanitize


def test_sanitize_drops_identity_keys_and_duplicate_content():
    raw = {
        "reviewId": 1,
        "content": "본문",
        "isRepurchase": True,
        "reviewType": "MONTH",
        "profileDto": {"memberNickname": "닉", "skinType": "건성"},
        "maskedWriterId": "ab***",
        "ownerName": "홍*동",
        "orderNo": "2026112100001",
        "goodsDto": {"optionName": "50ml", "userId": "u1"},
        "photos": [{"url": "https://img"}],
    }
    assert sanitize(raw, " 본문 ") == {
        "reviewId": 1,
        "isRepurchase": True,
        "reviewType": "MONTH",
        "goodsDto": {"optionName": "50ml"},
        "photos": [{"url": "https://img"}],
    }


def test_sanitize_rejects_non_dict_and_oversized():
    assert sanitize(["x"], "본문") is None
    assert sanitize({"content": "본문"}, "본문") is None
    assert sanitize({"blob": "x" * (MAX_BYTES + 1)}, "본문") is None


def test_raw_payload_is_not_serialized():
    review = Review(
        platform="t", product_id="p", review_id="r", content="본문", raw_payload={"a": 1}
    )
    assert "raw_payload" not in review.model_dump(mode="json")


async def test_raw_payload_is_stored_kept_and_does_not_invalidate_cache(session):
    await ProductRepository(session).upsert(
        Product(platform="test", product_id="p", name="상품", url="https://example.com")
    )
    repo = ReviewRepository(session)
    review = Review(
        platform="test",
        product_id="p",
        review_id="r",
        content="본문",
        raw_payload={"isRepurchase": True},
    )
    await repo.upsert_many("test", "p", [review])
    assert await session.scalar(select(ReviewRow.raw_payload)) == {"isRepurchase": True}

    await session.execute(update(ProductRow).values(analysis_input_hash="cached"))
    # 원본만 바뀌면 분석 입력 캐시는 그대로다.
    await repo.upsert_many(
        "test", "p", [review.model_copy(update={"raw_payload": {"isRepurchase": False}})]
    )
    assert await session.scalar(select(ReviewRow.raw_payload)) == {"isRepurchase": False}
    assert await session.scalar(select(ProductRow.analysis_input_hash)) == "cached"

    # 원본을 넘기지 않는 경로는 기존 원본을 지우지 않는다.
    await repo.upsert_many("test", "p", [review.model_copy(update={"raw_payload": None})])
    assert await session.scalar(select(ReviewRow.raw_payload)) == {"isRepurchase": False}
