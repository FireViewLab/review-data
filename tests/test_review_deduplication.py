"""중복 통합이 원본과 독립적인 리뷰를 보존하고 페이지·분석에 일관되게 적용된다."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from review_data.core.db.analysis_repository import AnalysisRepository, input_hash
from review_data.core.db.models import AnalysisJob, ProductRow, ReviewRow
from review_data.core.db.repository import ProductRepository, ReviewRepository
from review_data.core.models import Product, Review
from review_data.core.service.catalog import catalog_page
from review_data.core.settings import Settings

NOW = datetime(2026, 10, 9, tzinfo=UTC)


def review(review_id, **values):
    return Review(
        **{
            "platform": "kurly",
            "product_id": "p",
            "review_id": review_id,
            "content": "같은 상품을 재구매했고 품질이 만족스러워요",
            "author": "buyer",
            "written_at": NOW,
            "rating": 5,
            "option": "대용량",
            "images": ["https://x/image"],
            **values,
        }
    )


async def product(session, product_id="p"):
    await ProductRepository(session).upsert(
        Product(platform="kurly", product_id=product_id, name="상품", url="https://x")
    )


def settings():
    return Settings(_env_file=None, ai_analysis_enabled=True, ai_stream_url="http://x/stream")


async def test_duplicate_ids_in_one_batch_do_not_abort_storage(session):
    await product(session)
    repo = ReviewRepository(session)
    await repo.upsert_many("kurly", "p", [review("a"), review("a", content="수정된 리뷰")])
    rows, _ = await repo.list_page("kurly", "p")
    assert len(rows) == 1 and rows[0].content == "수정된 리뷰"


async def test_nul_characters_do_not_abort_review_storage(session):
    await product(session)
    await ReviewRepository(session).upsert_many(
        "kurly",
        "p",
        [
            review(
                "a",
                content="좋은\x00상품",
                author="buyer\x00",
                option="대\x00용량",
                images=["https://x/\x00image"],
            ),
            review("b", content="\x00"),
        ],
    )
    rows, _ = await ReviewRepository(session).list_page("kurly", "p")
    assert len(rows) == 1
    assert rows[0].content == "좋은상품" and rows[0].author == "buyer"
    assert rows[0].option == "대용량" and rows[0].images == ["https://x/image"]


async def test_identical_reviews_share_one_page_and_analysis_but_keep_originals(session):
    await product(session)
    repo = ReviewRepository(session)
    await repo.upsert_many("kurly", "p", [review("a"), review("b"), review("c", author="other")])
    assert await session.scalar(select(func.count()).select_from(ReviewRow)) == 3
    first, cursor = await repo.list_page("kurly", "p", limit=1)
    second, next_cursor = await repo.list_page("kurly", "p", limit=1, cursor=cursor)
    assert [r.review_id for r in first + second] == ["c", "a"]
    assert next_cursor is None
    analysis = AnalysisRepository(session)
    job_id = await analysis.enqueue("kurly", "p", settings())
    job = await session.get(AnalysisJob, job_id)
    assert [r["review_id"] for r in job.input_payload["reviews"]] == ["a", "c"]
    assert job.input_review_count == 2
    assert job.input_payload["sampling"]["source_review_count"] == 2


@pytest.mark.parametrize(
    "different",
    [
        {"author": "other"},
        {"author": None},
        {"author": " "},
        {"written_at": NOW + timedelta(days=1)},
        {"written_at": None},
        {"rating": 4},
        {"option": "소용량"},
        {"images": ["https://x/another"]},
        {"content": "다른 본문"},
    ],
)
async def test_independent_reviews_are_not_combined(session, different):
    await product(session)
    await ReviewRepository(session).upsert_many(
        "kurly", "p", [review("a"), review("b", **different)]
    )
    rows, cursor = await ReviewRepository(session).list_page("kurly", "p")
    assert len(rows) == 2 and cursor is None


@pytest.mark.parametrize("missing", [{"author": None}, {"author": " "}, {"written_at": None}])
async def test_reviews_with_same_missing_identity_remain_separate(session, missing):
    await product(session)
    await ReviewRepository(session).upsert_many(
        "kurly", "p", [review("a", **missing), review("b", **missing)]
    )
    rows, _ = await ReviewRepository(session).list_page("kurly", "p")
    assert len(rows) == 2


async def test_later_duplicate_preserves_representative_id_and_reuses_analysis(session):
    await product(session)
    session.add(
        ReviewRow(**review("z").model_dump(exclude={"collected_at"}), first_collected_at=NOW)
    )
    await session.flush()
    analysis = AnalysisRepository(session)
    job_id = await analysis.enqueue("kurly", "p", settings())
    session.add(
        ReviewRow(
            **review("a").model_dump(exclude={"collected_at"}),
            first_collected_at=NOW + timedelta(seconds=1),
        )
    )
    await session.flush()
    assert await analysis.enqueue("kurly", "p", settings()) == job_id
    rows, _ = await ReviewRepository(session).list_page("kurly", "p")
    assert [r.review_id for r in rows] == ["z"]


async def test_old_analysis_including_duplicates_is_replaced_without_forcing(session):
    await product(session)
    await ReviewRepository(session).upsert_many("kurly", "p", [review("a"), review("b")])
    raw = [
        {
            "review_id": rid,
            "content": review(rid).content,
            "rating": 5.0,
            "written_at": NOW.isoformat(),
        }
        for rid in ["a", "b"]
    ]
    old = AnalysisJob(
        platform="kurly",
        product_id="p",
        status="done",
        input_hash=input_hash(raw),
        input_review_count=2,
        input_payload={"reviews": raw, "model_version": None, "policy_version": None},
    )
    session.add(old)
    await session.flush()
    new_id = await AnalysisRepository(session).enqueue("kurly", "p", settings())
    await session.refresh(old)
    assert new_id != old.id and old.status == "done"
    new = await session.get(AnalysisJob, new_id)
    assert new.status == "queued" and new.input_review_count == 1
    assert await session.scalar(select(func.count()).select_from(ReviewRow)) == 2


async def test_empty_products_stay_stored_but_only_appear_after_a_review(session):
    for pid in ["a", "b", "c"]:
        await product(session, pid)
    await ReviewRepository(session).upsert_many("kurly", "b", [review("b", product_id="b")])
    rows, cursor = await catalog_page(session, settings(), 1)
    assert [p.product_id for p, _ in rows] == ["b"] and cursor is None
    assert await session.scalar(select(func.count()).select_from(ProductRow)) == 3
    await ReviewRepository(session).upsert_many("kurly", "c", [review("c", product_id="c")])
    rows, cursor = await catalog_page(session, settings(), 1)
    assert [p.product_id for p, _ in rows] == ["b"] and cursor is not None
    rows, cursor = await catalog_page(session, settings(), 1, cursor)
    assert [p.product_id for p, _ in rows] == ["c"] and cursor is None
