"""재수집 때 빠진 리뷰 표시: 전부 받아온 수집에서만 표시하고, 다시 보이면 지운다."""

from sqlalchemy import select

from review_data.core.db.models import ReviewRow
from review_data.core.db.repository import ProductRepository, ReviewRepository
from review_data.core.models import Product, Review


async def _seed(session, ids):
    await ProductRepository(session).upsert(
        Product(platform="test", product_id="p", name="상품", url="https://example.com")
    )
    repo = ReviewRepository(session)
    await repo.upsert_many(
        "test",
        "p",
        [Review(platform="test", product_id="p", review_id=i, content=f"본문 {i}") for i in ids],
    )
    return repo


async def _missing(session):
    rows = await session.execute(select(ReviewRow.review_id, ReviewRow.missing_since))
    return {rid for rid, since in rows if since is not None}


async def test_unseen_reviews_are_marked_and_cleared_when_seen_again(session):
    repo = await _seed(session, ["a", "b", "c", "d"])
    assert await repo.mark_missing("test", "p", {"a", "b", "c"}) == 1
    assert await _missing(session) == {"d"}

    # 이미 표시된 리뷰는 다시 세지 않는다.
    assert await repo.mark_missing("test", "p", {"a", "b", "c"}) == 0

    await repo.upsert_many(
        "test", "p", [Review(platform="test", product_id="p", review_id="d", content="본문 d")]
    )
    assert await _missing(session) == set()


async def test_empty_or_mass_missing_collection_marks_nothing(session):
    repo = await _seed(session, ["a", "b", "c", "d"])
    assert await repo.mark_missing("test", "p", set()) == 0
    # 4건 중 3건이 빠지면 차단·파싱 오류로 보고 표시하지 않는다.
    assert await repo.mark_missing("test", "p", {"a"}) == 0
    assert await _missing(session) == set()
