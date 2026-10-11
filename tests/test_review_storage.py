"""리뷰 재관측 캐시 보존과 바인드 한도/트랜잭션 검증."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from review_data.core.db.models import ProductRow, ReviewRow
from review_data.core.db.repository import ProductRepository, ReviewRepository
from review_data.core.models import Product, Review


async def test_unchanged_observation_preserves_timestamp_and_analysis_cache(session):
    await ProductRepository(session).upsert(
        Product(platform="test", product_id="p", name="상품", url="https://example.com")
    )
    review = Review(platform="test", product_id="p", review_id="r", content="본문", rating=4.1)
    repo = ReviewRepository(session)
    await repo.upsert_many("test", "p", [review])
    old = datetime.now(UTC) - timedelta(days=1)
    await session.execute(update(ReviewRow).values(updated_at=old, last_collected_at=old))
    await session.execute(update(ProductRow).values(analysis_input_hash="cached"))
    await repo.upsert_many("test", "p", [review])
    row = (await session.scalars(select(ReviewRow))).one()
    assert row.updated_at == old
    assert row.last_collected_at > old
    assert await session.scalar(select(ProductRow.analysis_input_hash)) == "cached"
    await repo.upsert_many("test", "p", [review.model_copy(update={"content": "수정"})])
    await session.refresh(row)
    assert row.content == "수정"
    assert row.updated_at > old
    assert await session.scalar(select(ProductRow.analysis_input_hash)) is None


async def test_large_review_batch_is_atomic_and_uses_last_duplicate(session):
    await ProductRepository(session).upsert(
        Product(platform="test", product_id="p", name="상품", url="https://example.com")
    )
    await session.commit()
    reviews = [
        Review(platform="test", product_id="p", review_id=str(i), content=f"본문 {i}")
        for i in range(3000)
    ]
    reviews.append(reviews[0].model_copy(update={"content": "마지막 관측"}))
    await ReviewRepository(session).upsert_many("test", "p", reviews)
    assert await session.scalar(select(func.count()).select_from(ReviewRow)) == 3000
    assert (
        await session.scalar(select(ReviewRow.content).where(ReviewRow.review_id == "0"))
        == "마지막 관측"
    )
    await session.rollback()
    assert await session.scalar(select(func.count()).select_from(ReviewRow)) == 0
