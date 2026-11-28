"""분석 입력 서명: 작업 생성 시 기록, SQL 계산과 일치, 재분석 대상 판정."""

from sqlalchemy import select, text

from review_data.core.db.analysis_repository import (
    AnalysisRepository,
    payload_signature,
    signature_sql,
    target_signature,
)
from review_data.core.db.models import AnalysisJob
from tests.test_analysis_pipeline import config, seed


def test_payload_signature_matches_target_fields():
    settings = config(ai_model_version="m", ai_policy_version="p", ai_max_reviews=500)
    payload = {
        "input_version": "image-count-v1",
        "model_version": "m",
        "policy_version": "p",
        "sampling": {"policy_version": "rating-stratified-v1", "max_reviews": 500},
    }
    assert payload_signature(payload) == target_signature(settings)
    assert payload_signature({**payload, "model_version": "other"}) != target_signature(settings)
    # 빈 값도 자리를 지켜 다른 필드와 섞이지 않는다.
    assert payload_signature({}) == "||||"


async def test_enqueue_records_signature_equal_to_sql(session_factory):
    settings = config(ai_model_version="m", ai_policy_version="p")
    await seed(session_factory, settings)
    async with session_factory() as s:
        job = await s.scalar(select(AnalysisJob))
        from_sql = await s.scalar(
            text(f"SELECT {signature_sql()} FROM analysis_jobs WHERE id = :id"), {"id": job.id}
        )
    assert job.input_signature == target_signature(settings) == from_sql


async def test_same_input_reuses_job_after_signature_added(session_factory):
    settings = config(ai_model_version="m", ai_policy_version="p")
    job_id = await seed(session_factory, settings)
    async with session_factory() as s:
        assert await AnalysisRepository(s).enqueue("kurly", "p", settings) == job_id


async def _campaign_items(session_factory, settings):
    from review_data.core.db.models import AnalysisCampaignItem
    from tests.test_analysis_refresh import tick

    await tick(session_factory, settings)
    async with session_factory() as s:
        return sorted((await s.execute(select(AnalysisCampaignItem.product_id))).scalars().all())


async def test_campaign_skips_current_jobs_with_and_without_signature(session_factory):
    from sqlalchemy import update

    from tests.test_analysis_refresh import config as refresh_config
    from tests.test_analysis_refresh import seed as refresh_seed

    await refresh_seed(session_factory, count=3, version="v1")
    async with session_factory() as s:
        # 상품 0은 옛 작업처럼 서명이 없다. JSON을 읽어 같은 결론을 내야 한다.
        await s.execute(
            update(AnalysisJob).where(AnalysisJob.product_id == "0").values(input_signature=None)
        )
        # 상품 1은 서명이 다른 기준이다. 재분석 대상이 된다.
        await s.execute(
            update(AnalysisJob).where(AnalysisJob.product_id == "1").values(input_signature="old")
        )
        await s.commit()
    assert await _campaign_items(session_factory, refresh_config("v1")) == ["1"]
