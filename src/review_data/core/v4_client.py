"""v4.0 분석 서비스(/v4/analyze) 클라이언트.

같은 상품의 리뷰 묶음을 보내고 리뷰별 의심 확률·텍스트 점수·정책 지수·등급을 받는다.
응답은 저장 전에 입력과 같은 리뷰인지, 값이 범위 안인지 확인한다.
"""

from dataclasses import dataclass

import httpx

LEVELS = frozenset({"SAFE", "WARN", "DANGER"})


class V4Error(Exception):
    """kind·retryable은 분석 워커의 실패 원인·재시도 판단에 쓴다. 메시지는 저장하지 않는다."""

    def __init__(self, message: str, *, kind: str = "validation", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


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
    except httpx.TimeoutException as exc:
        raise V4Error("v4 응답 시간 초과", kind="timeout", retryable=True) from exc
    except httpx.HTTPError as exc:
        raise V4Error(
            f"v4 호출 실패: {type(exc).__name__}", kind="transport", retryable=True
        ) from exc
    finally:
        if owned:
            await client.aclose()
    if response.status_code != 200:
        # 인증·주소 설정 오류(401/403/404)도 다시 보내면 낫는 운영 문제다. 작업을 실패로 버리지 않고
        # 재시도로 남겨 장애 감지가 워커를 멈추게 한다. 요청 형식 오류(422 등)만 재시도하지 않는다.
        retryable = response.status_code >= 500 or response.status_code in (401, 403, 404, 408, 429)
        raise V4Error(f"v4 응답 상태 {response.status_code}", kind="http", retryable=retryable)
    try:
        data = response.json()
    except ValueError as exc:
        raise V4Error("v4 응답이 JSON이 아닙니다.") from exc
    return parse(data, {r["review_id"] for r in body["reviews"]})


class V4AnalysisClient:
    """분석 워커용 v4 클라이언트. AnalysisStreamClient와 같은 analyze 시그니처를 쓴다."""

    contract_version = "v4"

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float,
        model_version: str | None,
        policy_version: str | None,
        client: httpx.AsyncClient | None = None,
    ):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.model_version = model_version
        self.policy_version = policy_version
        self.client = client

    async def analyze(
        self, platform: str, product_id: str, reviews: list[dict], job_id: int, input_hash: str
    ) -> V4Response:
        response = await analyze(
            self.url,
            self.token,
            platform,
            product_id,
            reviews,
            timeout=self.timeout,
            client=self.client,
        )
        if not response.results:
            # 본문이 있는 리뷰가 없으면 호출하지 않는다. 빈 결과를 목표 버전으로 저장한다.
            return V4Response(self.model_version or "", self.policy_version or "", [])
        return response
