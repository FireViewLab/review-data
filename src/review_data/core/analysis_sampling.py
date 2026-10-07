"""별점 분포와 극값을 보존하는 결정적 대표 표본을 선택한다."""

import math
from collections import Counter, defaultdict
from fractions import Fraction

SAMPLING_VERSION = "rating-stratified-v1"


def _bucket(review: dict) -> str:
    rating = review.get("rating")
    if rating is None:
        return "unrated"
    if not math.isfinite(rating):
        return "invalid_rating"
    if 0 <= rating <= 5:
        return str(math.ceil(rating))
    return "outside_scale"


def _order(review: dict):
    return review.get("written_at") or "", review["review_id"]


def select_reviews(reviews: list[dict], limit: int) -> tuple[list[dict], dict]:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("분석 상한은1~500의 정수여야 한다.")
    groups = defaultdict(list)
    for review in reviews:
        groups[_bucket(review)].append(review)
    sampled = len(reviews) > limit
    if not sampled:
        selected = sorted(reviews, key=lambda row: row["review_id"])
    else:
        selected = _sample(groups, limit, len(reviews))
    metadata = {
        "sampled": sampled,
        "method": "rating_stratified" if sampled else "all",
        "policy_version": SAMPLING_VERSION,
        "max_reviews": limit,
        "source_review_count": len(reviews),
        "analyzed_review_count": len(selected),
        "source_rating_distribution": dict(
            sorted((key, len(rows)) for key, rows in groups.items())
        ),
        "sample_rating_distribution": dict(
            sorted(Counter(_bucket(row) for row in selected).items())
        ),
    }
    return selected, metadata


def _sample(groups: dict[str, list[dict]], limit: int, total: int) -> list[dict]:
    mandatory = defaultdict(list)
    rated = [
        row
        for rows in groups.values()
        for row in rows
        if row.get("rating") is not None and math.isfinite(row["rating"])
    ]
    if rated:
        low = min(rated, key=lambda row: (row["rating"], *_order(row)))
        high = max(rated, key=lambda row: (row["rating"], *_order(row)))
        mandatory[_bucket(low)].append(low)
        if limit >= 2 and high["rating"] != low["rating"]:
            mandatory[_bucket(high)].append(high)
    lower = {key: len(mandatory[key]) for key in groups}
    for key in sorted(groups):
        if sum(lower.values()) < limit:
            lower[key] = max(lower[key], 1)
    ideal = {key: Fraction(limit * len(rows), total) for key, rows in groups.items()}
    quotas = {key: max(lower[key], int(ideal[key])) for key in groups}
    while sum(quotas.values()) > limit:
        key = max(
            (key for key in groups if quotas[key] > lower[key]),
            key=lambda key: (quotas[key] - ideal[key], key),
        )
        quotas[key] -= 1
    while sum(quotas.values()) < limit:
        key = max(
            (key for key in groups if quotas[key] < len(groups[key])),
            key=lambda key: (ideal[key] - quotas[key], key),
        )
        quotas[key] += 1
    selected = []
    for key in sorted(groups):
        fixed = mandatory[key]
        fixed_ids = {row["review_id"] for row in fixed}
        remaining = sorted(
            (row for row in groups[key] if row["review_id"] not in fixed_ids), key=_order
        )
        count = quotas[key] - len(fixed)
        if count == 1:
            indices = [len(remaining) // 2]
        elif count > 1:
            indices = [index * (len(remaining) - 1) // (count - 1) for index in range(count)]
        else:
            indices = []
        selected.extend(fixed)
        selected.extend(remaining[index] for index in indices)
    return sorted(selected, key=lambda row: row["review_id"])
