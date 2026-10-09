"""원본 행을 보존하면서 조회와 분석에 동일한 대표 리뷰 집합을 제공한다."""

from sqlalchemy import case, func, select
from sqlalchemy.orm import aliased

from review_data.core.db.models import ReviewRow


def representative_reviews(platform: str, product_id: str):
    # 작성자·작성일이 없는 리뷰는 서로 다른 사람의 짧은 문구일 수 있다.
    # 두 근거가 모두 있을 때만 나머지 필드가 완전히 같은 리뷰를 묶는다.
    known = (
        ReviewRow.author.is_not(None)
        & (func.btrim(ReviewRow.author) != "")
        & ReviewRow.written_at.is_not(None)
    )
    ranked = (
        select(
            ReviewRow,
            func.row_number()
            .over(
                partition_by=[
                    ReviewRow.content,
                    ReviewRow.rating,
                    ReviewRow.author,
                    ReviewRow.written_at,
                    ReviewRow.option,
                    ReviewRow.images,
                    case((known, None), else_=ReviewRow.review_id),
                ],
                # 처음 저장된 ID를 유지한다. 이후 작은 ID가 추가돼도 바뀌지 않는다.
                order_by=[ReviewRow.first_collected_at, ReviewRow.review_id],
            )
            .label("duplicate_rank"),
        )
        .where(ReviewRow.platform == platform, ReviewRow.product_id == product_id)
        .subquery()
    )
    representative = aliased(ReviewRow, ranked)
    return representative, select(representative).where(ranked.c.duplicate_rank == 1)
