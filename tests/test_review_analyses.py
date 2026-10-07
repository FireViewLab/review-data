"""리뷰 결과의 실행별 보존, 원본 연결, 계약 제약과 마이그레이션을 검증한다."""

from decimal import Decimal
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from review_data.core.db.models import AnalysisJob, ProductRow, ReviewAnalysisRow, ReviewRow

from .conftest import DATABASE_URL


async def _seed(session):
    for platform, product in [("kurly", "p1"), ("kurly", "p2"), ("naver", "p1")]:
        session.add(ProductRow(platform=platform, product_id=product, name="상품", url="https://x"))
    await session.flush()
    for platform, product in [("kurly", "p1"), ("kurly", "p2"), ("naver", "p1")]:
        session.add(
            ReviewRow(platform=platform, product_id=product, review_id="r1", content="좋음")
        )
    session.add_all(
        [
            AnalysisJob(id=101, platform="kurly", product_id="p1", status="done"),
            AnalysisJob(id=102, platform="kurly", product_id="p1", status="done"),
        ]
    )
    await session.flush()


def _result(job=101, **values):
    return dict(analysis_job_id=job, platform="kurly", product_id="p1", review_id="r1", **values)


async def test_null_scores_and_empty_reasons_are_preserved(session):
    await _seed(session)
    row = ReviewAnalysisRow(**_result())
    session.add(row)
    await session.flush()
    await session.refresh(row)
    assert (row.rti, row.level, row.text_score, row.behavior_score, row.network_score) == (
        None,
    ) * 5
    assert row.reasons == []
    assert row.model_version is None and row.input_hash is None
    assert row.created_at.tzinfo is not None


async def test_runs_preserve_precision_and_history_when_review_changes(session):
    await _seed(session)
    session.add_all(
        [
            ReviewAnalysisRow(
                **_result(
                    rti=Decimal("82.4123456789"),
                    level="safe",
                    text_score=Decimal("85.123456789"),
                    network_score=Decimal("76.0"),
                    reasons=["FUTURE_REASON_CODE"],
                    model_version="rti-0.5",
                    input_hash="old-input",
                )
            ),
            ReviewAnalysisRow(**_result(102, rti=Decimal("0"), level="danger")),
        ]
    )
    await session.flush()
    await session.execute(update(ReviewRow).values(content="수정된 리뷰"))
    session.expire_all()
    rows = list(
        (
            await session.scalars(
                select(ReviewAnalysisRow).order_by(ReviewAnalysisRow.analysis_job_id)
            )
        ).all()
    )
    assert len(rows) == 2
    assert rows[0].rti == Decimal("82.4123456789")
    assert rows[0].text_score == Decimal("85.123456789")
    assert rows[0].behavior_score is None
    assert rows[0].reasons == ["FUTURE_REASON_CODE"]
    assert (rows[0].model_version, rows[0].input_hash) == ("rti-0.5", "old-input")
    assert rows[1].rti == 0


async def test_same_run_cannot_store_duplicate_result(session):
    await _seed(session)
    await session.execute(ReviewAnalysisRow.__table__.insert().values(**_result()))
    with pytest.raises(IntegrityError):
        await session.execute(ReviewAnalysisRow.__table__.insert().values(**_result()))


async def test_reasons_cannot_be_sql_null(session):
    await _seed(session)
    fields = _result(reasons=None)
    with pytest.raises(IntegrityError):
        await session.execute(ReviewAnalysisRow.__table__.insert().values(**fields))


@pytest.mark.parametrize(
    "values",
    [
        {"analysis_job_id": 999},
        {"review_id": "missing"},
        {"product_id": "p2"},
        {"platform": "naver"},
    ],
)
async def test_result_must_match_job_product_and_existing_review(session, values):
    await _seed(session)
    fields = _result()
    fields.update(values)
    session.add(ReviewAnalysisRow(**fields))
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.parametrize(
    "values",
    [
        {"rti": Decimal("-0.001")},
        {"rti": Decimal("100.001")},
        {"rti": Decimal("NaN")},
        {"level": "unknown"},
        {"reasons": [None]},
    ],
)
async def test_invalid_contract_values_are_rejected(session, values):
    await _seed(session)
    session.add(ReviewAnalysisRow(**_result(**values)))
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.parametrize("rti,level", [(0, "danger"), (100, "safe"), (50, "warn")])
async def test_rti_boundaries_and_levels(session, rti, level):
    await _seed(session)
    row = ReviewAnalysisRow(**_result(rti=rti, level=level))
    session.add(row)
    await session.flush()
    await session.refresh(row)
    assert (row.rti, row.level) == (rti, level)


@pytest.mark.parametrize("parent", [AnalysisJob, ReviewRow, ProductRow])
async def test_deleting_parent_cascades_to_results(session, parent):
    await _seed(session)
    session.add(ReviewAnalysisRow(**_result()))
    await session.flush()
    await session.execute(delete(parent))
    assert (await session.scalar(select(ReviewAnalysisRow))) is None


async def test_migration_round_trip_preserves_existing_data(engine):
    """트랜잭션 내 독립 스키마에서 적용·롤백해 기존 테스트 테이블을 보존한다."""
    eng = create_async_engine(DATABASE_URL)
    schema = "analysis_migration_" + uuid4().hex
    try:
        async with eng.connect() as conn:
            transaction = await conn.begin()
            try:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))

                def run(sync_conn):
                    config = Config()
                    config.set_main_option(
                        "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
                    )
                    scripts = ScriptDirectory.from_config(config)
                    revisions = list(
                        reversed(list(scripts.iterate_revisions("c6f4a28d901b", "base")))
                    )
                    with Operations.context(MigrationContext.configure(sync_conn)):
                        for revision in revisions[:-1]:
                            revision.module.upgrade()
                        sync_conn.execute(
                            text(
                                "INSERT INTO products(platform, product_id, name, url) "
                                "VALUES ('kurly', 'p1', '기존 상품', 'https://x')"
                            )
                        )
                        sync_conn.execute(
                            text(
                                "INSERT INTO reviews(platform, product_id, review_id, content) "
                                "VALUES ('kurly', 'p1', 'r1', '기존 리뷰')"
                            )
                        )
                        sync_conn.execute(
                            text(
                                "INSERT INTO analysis_jobs(id, platform, product_id, result) "
                                "VALUES (101, 'kurly', 'p1', '{\"legacy\": true}')"
                            )
                        )
                        revisions[-1].module.upgrade()
                        inspector = inspect(sync_conn)
                        columns = inspector.get_columns("review_analyses")
                        assert {c["name"] for c in columns} == set(
                            ReviewAnalysisRow.__table__.columns.keys()
                        )
                        assert inspector.get_pk_constraint("review_analyses")[
                            "constrained_columns"
                        ] == ["analysis_job_id", "review_id"]
                        assert len(inspector.get_foreign_keys("review_analyses")) == 2
                        assert len(inspector.get_check_constraints("review_analyses")) == 3
                        sync_conn.execute(
                            text(
                                "INSERT INTO review_analyses(analysis_job_id, platform, "
                                "product_id, "
                                "review_id) VALUES (101, 'kurly', 'p1', 'r1')"
                            )
                        )
                        assert sync_conn.scalar(text("SELECT reasons FROM review_analyses")) == []
                        revisions[-1].module.downgrade()
                        assert not inspect(sync_conn).has_table("review_analyses")
                        assert "uq_analysis_jobs_identity" not in {
                            c["name"]
                            for c in inspect(sync_conn).get_unique_constraints("analysis_jobs")
                        }
                        assert sync_conn.scalar(text("SELECT content FROM reviews")) == "기존 리뷰"
                        assert sync_conn.scalar(text("SELECT result FROM analysis_jobs")) == {
                            "legacy": True
                        }
                        revisions[-1].module.upgrade()
                        assert sync_conn.scalar(text("SELECT count(*) FROM review_analyses")) == 0

                await conn.run_sync(run)
            finally:
                await transaction.rollback()
    finally:
        await eng.dispose()


def test_migration_generates_upgrade_and_downgrade_sql():
    """DB가 없는 환경에서도 revision 연결과 PostgreSQL DDL 생성을 확인한다."""
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    command.upgrade(config, "b1cf29089ede:head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE review_analyses" in sql
    assert "FOREIGN KEY(analysis_job_id, platform, product_id)" in sql
    assert "FOREIGN KEY(platform, product_id, review_id)" in sql
    assert "ON DELETE CASCADE" in sql
    assert "CREATE INDEX idx_review_analyses_review" in sql
    assert "ADD CONSTRAINT uq_analysis_jobs_identity UNIQUE" in sql
    output.truncate(0)
    output.seek(0)
    command.downgrade(config, "head:b1cf29089ede", sql=True)
    sql = output.getvalue()
    assert "DROP TABLE review_analyses" in sql
    assert "DROP CONSTRAINT uq_analysis_jobs_identity" in sql
