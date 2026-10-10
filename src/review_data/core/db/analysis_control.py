"""대시보드에서 확정한 분석 목표를 모든 서비스가 DB에서 공유한다."""

from sqlalchemy import select

from review_data.core.db.models import AnalysisRefreshControl
from review_data.core.settings import Settings


async def effective_analysis_settings(session, settings: Settings) -> Settings:
    row = await session.get(AnalysisRefreshControl, 1, populate_existing=True)
    if row is None:
        return settings
    return settings.model_copy(
        update={
            "ai_model_version": row.model_version,
            "ai_policy_version": row.policy_version,
            "analysis_refresh_enabled": row.enabled,
        }
    )


async def control_status(session, settings: Settings) -> dict:
    row = await session.scalar(select(AnalysisRefreshControl).where(AnalysisRefreshControl.id == 1))
    return {
        "source": "dashboard" if row else "environment",
        "enabled": row.enabled if row else settings.analysis_refresh_enabled,
        "model_version": row.model_version if row else settings.ai_model_version,
        "policy_version": row.policy_version if row else settings.ai_policy_version,
        "updated_at": row.updated_at if row else None,
    }
