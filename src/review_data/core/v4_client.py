"""review-model v4.0 분석 서비스(/v4/analyze) 클라이언트.

같은 상품의 리뷰 묶음을 보내고 리뷰별 의심 확률·텍스트 점수·정책 지수·등급을 받는다.
응답은 저장 전에 입력과 같은 리뷰인지, 값이 범위 안인지 확인한다.
"""

from dataclasses import dataclass

import httpx

LEVELS = frozenset({"SAFE", "WARN", "DANGER"})


class V4Error(Exception):
    pass


@dataclass(frozen=True)
class V4Result:
    review_id: str
    text_probability: float
    text_score: float
    policy_index: float
    level: str


@dataclass(frozen=True)
class V4Response:
    model_version: str
    policy_version: str
    results: list[V4Result]


def parse(data: dict, expected_ids: set[str]) -> V4Response:
    try:
        results = [
            V4Result(
                review_id=str(r["review_id"]),
                text_probability=float(r["text_probability"]),
                text_score=float(r["text_score"]),
                policy_index=float(r["policy_index"]),
                level=str(r["level"]),
            )
            for r in data["results"]
        ]
        response = V4Response(str(data["model_version"]), str(data["policy_version"]), results)
    except (KeyError, TypeError, ValueError) as exc:
        raise V4Error(f"v4 응답 형식 오류: {exc}") from exc
    ids = [r.review_id for r in results]
    if len(ids) != len(set(ids)) or set(ids) != expected_ids:
        raise V4Error("v4 결과의 리뷰 식별자가 입력과 다릅니다.")
    for r in results:
        if (
            r.level not in LEVELS
            or not 0 <= r.text_probability <= 1
            or not 0 <= r.policy_index <= 100
            or not 0 <= r.text_score <= 100
        ):
            raise V4Error(f"v4 결과 값이 범위를 벗어났습니다: {r.review_id}")
    return response


async def analyze(
    url: str,
    token: str,
    platform: str,
    product_id: str,
    reviews: list[dict],
    *,
    timeout: float = 120.0,
    client: httpx.AsyncClient | None = None,
) -> V4Response:
    """reviews: [{"review_id", "content"}]. 본문이 빈 리뷰는 보내지 않는다."""
    body = {
        "platform": platform,
        "product_id": product_id,
        "reviews": [
            {"review_id": str(r["review_id"]), "content": r["content"]}
            for r in reviews
            if (r.get("content") or "").strip()
        ],
    }
    if not body["reviews"]:
        return V4Response("", "", [])
    owned = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await client.post(url, json=body, headers={"X-Internal-Token": token})
    except httpx.HTTPError as exc:
        raise V4Error(f"v4 호출 실패: {type(exc).__name__}") from exc
    finally:
        if owned:
            await client.aclose()
    if response.status_code != 200:
        raise V4Error(f"v4 응답 상태 {response.status_code}")
    return parse(response.json(), {r["review_id"] for r in body["reviews"]})
