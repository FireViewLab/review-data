"""운영 집계에서 최신 상태·빈 상품 원인을 구분하고 비밀/본문을 숨긴다."""

import json
from datetime import UTC, datetime, timedelta

from review_data.core.db.models import CollectionJob, ProductRow
from review_data.core.service.operations import operational_status


async def test_empty_reasons_and_expired_leases_are_reported_without_errors(session):
    now = datetime.now(UTC)
    for pid in ["empty", "pending", "failed", "missing", "expired"]:
        session.add(
            ProductRow(
                platform="kurly",
                product_id=pid,
                name="상품",
                url="https://x",
                review_count=10,
                reviews_last_collected_at=now if pid == "empty" else None,
            )
        )
    await session.flush()
    for pid, status, review_status in [
        ("empty", "succeeded", "succeeded"),
        ("pending", "pending", "pending"),
        ("failed", "failed", "failed"),
        ("expired", "running", "pending"),
    ]:
        session.add(
            CollectionJob(
                platform="kurly",
                product_id=pid,
                idempotency_key=pid,
                status=status,
                review_status=review_status,
                last_error="secret-token-review-content",
                lease_expires_at=now - timedelta(seconds=10) if pid == "expired" else None,
                attempt_count=3 if pid == "expired" else 0,
            )
        )
    await session.flush()
    result = await operational_status(session)
    assert result["queues"]["collection"]["waiting"] == 1
    assert result["queues"]["collection"]["expired_leases"] == 1
    assert result["queues"]["collection"]["exhausted_leases"] == 1
    reasons = {x["reason"]: x["count"] for x in result["empty_products"]}
    assert reasons == {
        "collected_empty": 1,
        "collection_failed": 1,
        "not_collected": 1,
        "pending": 1,
        "running": 1,
    }
    assert "secret-token" not in json.dumps(result, default=str)
