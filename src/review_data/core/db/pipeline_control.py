"""수집·AI 분석 요청 중지 상태. 외부 장애 때 운영자가 /status 에서 바꾼다."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from review_data.core.db.models import PipelineControl

TARGETS = ("collection", "analysis")


async def pipeline_state(session) -> dict:
    row = await session.scalar(select(PipelineControl).where(PipelineControl.id == 1))
    if row is None:
        return {
            "collection_paused": False,
            "analysis_paused": False,
            "collection_updated_at": None,
            "analysis_updated_at": None,
        }
    return {
        "collection_paused": row.collection_paused,
        "analysis_paused": row.analysis_paused,
        "collection_updated_at": row.collection_updated_at,
        "analysis_updated_at": row.analysis_updated_at,
    }


async def collection_stopped(session) -> bool:
    return (await pipeline_state(session))["collection_paused"]


async def analysis_stopped(session) -> bool:
    return (await pipeline_state(session))["analysis_paused"]


async def set_paused(session, target: str, paused: bool) -> dict:
    if target not in TARGETS:
        raise ValueError(f"알 수 없는 대상: {target}")
    values = {f"{target}_paused": paused, f"{target}_updated_at": datetime.now(UTC)}
    await session.execute(
        insert(PipelineControl)
        .values(id=1, **values)
        .on_conflict_do_update(index_elements=["id"], set_=values)
    )
    await session.flush()
    return await pipeline_state(session)
