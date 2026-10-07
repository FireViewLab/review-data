"""분포·극값·표본 상한과 재현성을 검증한다."""

import copy
import random
from collections import Counter

import pytest

from review_data.core.analysis_sampling import select_reviews


def reviews(ratings):
    return [
        {
            "review_id": f"r{index:06}",
            "content": "리뷰",
            "rating": rating,
            "written_at": f"2026-10-{index % 28 + 1:02}T00:00:00+00:00",
        }
        for index, rating in enumerate(ratings)
    ]


@pytest.mark.parametrize("count", [0, 1, 499, 500, 501, 2000])
async def test_limit_boundary_and_unique_original_rows(count):
    source = reviews([5] * count)
    original = copy.deepcopy(source)
    selected, meta = select_reviews(source, 500)
    assert len(selected) == min(count, 500)
    assert len({row["review_id"] for row in selected}) == len(selected)
    assert all(row in source for row in selected)
    assert source == original
    assert meta["sampled"] is (count > 500)
    assert meta["source_review_count"] == count
    assert meta["analyzed_review_count"] == len(selected)
    assert sum(meta["source_rating_distribution"].values()) == count
    assert sum(meta["sample_rating_distribution"].values()) == len(selected)


def test_proportional_distribution_and_minimum_maximum_scores():
    source = reviews([1] * 100 + [2] * 200 + [3] * 300 + [4] * 500 + [5] * 800 + [None] * 100)
    selected, meta = select_reviews(source, 500)
    assert Counter(row["rating"] for row in selected) == {
        1: 25,
        2: 50,
        3: 75,
        4: 125,
        5: 200,
        None: 25,
    }
    assert meta["sample_rating_distribution"] == {
        "1": 25,
        "2": 50,
        "3": 75,
        "4": 125,
        "5": 200,
        "unrated": 25,
    }


def test_rare_extremes_and_unrated_are_not_lost_to_rounding():
    selected, meta = select_reviews(reviews([5] * 9998 + [1, None]), 500)
    assert Counter(row["rating"] for row in selected) == {5: 498, 1: 1, None: 1}
    assert meta["source_review_count"] == 10000


def test_extremes_inside_same_fractional_rating_bucket_are_preserved():
    selected, meta = select_reviews(reviews([4.5] * 998 + [4.01, 4.99]), 500)
    assert {4.01, 4.99}.issubset({row["rating"] for row in selected})
    assert meta["sample_rating_distribution"] == {"5": 500}


def test_input_order_does_not_change_selection_or_distribution():
    source = reviews([1, 2, 3, 4, 5, None] * 300)
    selected, meta = select_reviews(source, 500)
    random.Random(1).shuffle(source)
    assert select_reviews(source, 500) == (selected, meta)


def test_dates_spread_within_the_rating_group():
    source = reviews([5] * 1000)
    selected, _ = select_reviews(source, 100)
    assert len({row["written_at"] for row in selected}) >= 25


@pytest.mark.parametrize("limit", [1, 2, 3, 6, 7, 20, 499, 500])
def test_small_caps_and_imbalanced_fractional_ratings(limit):
    source = reviews([None] * 1500 + [0, 0.1, 0.9, 1.5, 2.5, 3.5, 4.1, 4.9])
    selected, _ = select_reviews(source, limit)
    assert len(selected) == limit
    assert len({row["review_id"] for row in selected}) == limit
    assert any(row["rating"] == 0 for row in selected)
    if limit >= 2:
        assert any(row["rating"] == 4.9 for row in selected)


def test_distributions_stay_close_to_population_across_varied_inputs():
    generator = random.Random(92)
    for _ in range(30):
        sizes = [generator.randint(1, 1500) for _ in range(7)]
        source = reviews(
            [
                rating
                for rating, size in zip([0, 1, 2, 3, 4, 5, None], sizes, strict=True)
                for _ in range(size)
            ]
        )
        selected, _ = select_reviews(source, 500)
        selected_counts = Counter(row["rating"] for row in selected)
        assert len(selected) == 500
        for rating, size in zip([0, 1, 2, 3, 4, 5, None], sizes, strict=True):
            assert selected_counts[rating] >= 1
            assert abs(selected_counts[rating] - 500 * size / len(source)) < 2


@pytest.mark.parametrize("limit", [0, -1, 501, True, 1.5])
def test_invalid_limits_rejected(limit):
    with pytest.raises(ValueError):
        select_reviews(reviews([5]), limit)


def test_two_rare_extremes_in_one_bucket_get_two_slots():
    selected, meta = select_reviews(reviews([None] * 9998 + [4.01, 4.99]), 500)
    assert {4.01, 4.99}.issubset({row["rating"] for row in selected})
    assert meta["sample_rating_distribution"] == {"5": 2, "unrated": 498}
