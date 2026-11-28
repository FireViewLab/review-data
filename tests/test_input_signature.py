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
