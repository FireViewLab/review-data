"""옛 분석 작업의 입력 서명을 작은 묶음으로 나눠 채운다.

서명 컬럼을 추가하기 전에 만든 작업은 서명이 비어 있어, 재분석 대상을 고를 때 input_payload를 다시
연다. 한 번에 모두 채우면 수십 분 동안 큰 JSON을 읽으며 잠금을 쥐므로 묶음마다 따로 커밋한다.
"""

from sqlalchemy import text

from review_data.core.db.analysis_repository import signature_sql

_FILL = f"""
UPDATE analysis_jobs SET input_signature = {signature_sql()}
WHERE id IN (
    SELECT id FROM analysis_jobs
    WHERE input_signature IS NULL AND input_payload IS NOT NULL
    ORDER BY id LIMIT :limit
    FOR UPDATE SKIP LOCKED
)
"""


async def fill_batch(session, batch_size: int) -> int:
    """서명이 빈 작업 batch_size개를 채우고 채운 수를 돌려준다. 커밋은 호출한 쪽이 한다."""
    return (await session.execute(text(_FILL), {"limit": batch_size})).rowcount


async def fill_all(factory, *, batch_size: int = 500, max_batches: int = 0) -> dict:
    filled = batches = 0
    while not max_batches or batches < max_batches:
        async with factory() as session:
            count = await fill_batch(session, batch_size)
            await session.commit()
        if not count:
            break
        filled += count
        batches += 1
    async with factory() as session:
        remaining = await session.scalar(
            text(
                "SELECT count(*) FROM analysis_jobs "
                "WHERE input_signature IS NULL AND input_payload IS NOT NULL"
            )
        )
    return {"filled": filled, "batches": batches, "remaining": remaining}
