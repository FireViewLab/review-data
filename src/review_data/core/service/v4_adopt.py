"""v4 단독 전환: v4 결과가 이미 채워진 완료 작업을 목표 버전 v4의 결과로 인정한다.

v3.1 계약 시절에 v4 결과를 함께 채운 작업은 같은 입력의 v4 결과를 이미 갖고 있다.
목표 버전을 v4로 바꾸면 이 작업들이 '옛 버전'으로 보여 전부 재분석 대상이 된다.
그래서 작업의 버전 표시만 v4로 옮긴다.
리뷰별 결과 행과 v3.1 칸 값은 지우지 않고, 옮기기 전 버전은 result.adopted_from에 남긴다.
같은 트랜잭션에서 대시보드 목표 버전도 바꿔 재분석 예약이 그 사이에 옛 기준으로 돌지 않게 한다.
"""

from sqlalchemy import func, select, text

from review_data.core.db.models import AnalysisRefreshControl
from review_data.core.service.scheduling import _SCHEDULER_LOCK_KEY

_CANDIDATES = """
FROM analysis_jobs j
WHERE j.status = 'done' AND j.v4_completed_at IS NOT NULL
  AND (j.model_version IS DISTINCT FROM :model OR j.policy_version IS DISTINCT FROM :policy)
  AND EXISTS (SELECT 1 FROM review_analyses a WHERE a.analysis_job_id = j.id
              AND a.model_version_v4 = :model AND a.policy_version_v4 = :policy)
"""


async def adopt(session, *, model_version: str, policy_version: str, dry_run: bool = False) -> dict:
    params = {"model": model_version, "policy": policy_version}
    await session.execute(select(func.pg_advisory_xact_lock(_SCHEDULER_LOCK_KEY)))
    candidates = await session.scalar(text("SELECT count(*) " + _CANDIDATES), params)
    if dry_run:
        return {"dry_run": True, "candidates": candidates}
    adopted = (
        await session.execute(
            text(
                """
                UPDATE analysis_jobs t SET
                    result = coalesce(t.result, '{}'::jsonb) || jsonb_build_object(
                        'adopted_from', jsonb_build_object(
                            'model_version', t.model_version, 'policy_version', t.policy_version)),
                    model_version = :model,
                    policy_version = :policy,
                    input_payload = t.input_payload || jsonb_build_object(
                        'model_version', :model, 'policy_version', :policy),
                    updated_at = now()
                WHERE t.id IN (SELECT j.id """
                + _CANDIDATES
                + ")"
            ),
            params,
        )
    ).rowcount
    control = await session.get(AnalysisRefreshControl, 1, with_for_update=True)
    if control is not None:
        control.model_version = model_version
        control.policy_version = policy_version
    return {
        "dry_run": False,
        "candidates": candidates,
        "adopted": adopted,
        "dashboard_target_updated": control is not None,
    }
