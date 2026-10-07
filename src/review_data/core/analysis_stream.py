"""제안된 분석 SSE 계약을 검증한다. 정상 done 및 EOF 전에는 결과를 반환하지 않는다."""

import json
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import httpx

MAX_FRAME_BYTES = 1024 * 1024
_SCORES = ("rti", "text_score", "behavior_score", "network_score")
_RESULT_FIELDS = {"review_id", "level", "reasons", *_SCORES}


class AnalysisStreamError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class AnalysisStreamResult:
    results: list[dict]
    model_version: str | None = None
    policy_version: str | None = None
    ai_job_id: str | None = None


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisStreamError(f"{field} must be a nonempty string")
    return value


def _header(value: str, field: str) -> str:
    if any(ord(char) < 32 or ord(char) >= 127 for char in value):
        raise AnalysisStreamError(f"{field} must contain printable ASCII only")
    return value


def _number(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise AnalysisStreamError(f"{field} must be a finite number or null")
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite():
        raise AnalysisStreamError(f"{field} must be a finite number or null")
    return number


def _reviews(reviews: list[dict]) -> list[dict]:
    if not isinstance(reviews, list) or not reviews:
        raise AnalysisStreamError("reviews must be a nonempty list")
    payload = []
    seen = set()
    for item in reviews:
        if not isinstance(item, dict):
            raise AnalysisStreamError("review must be an object")
        review_id = _nonempty(item.get("review_id"), "review_id")
        if review_id in seen:
            raise AnalysisStreamError("duplicate request review_id")
        seen.add(review_id)
        content = _nonempty(item.get("content"), "content")
        rating = _number(item.get("rating"), "rating")
        json_rating = float(rating) if rating is not None else None
        if json_rating is not None and not math.isfinite(json_rating):
            raise AnalysisStreamError("rating cannot be represented as a finite JSON number")
        written_at = item.get("written_at")
        if isinstance(written_at, datetime):
            written_at = written_at.isoformat()
        elif written_at is not None and not isinstance(written_at, str):
            raise AnalysisStreamError("written_at must be a datetime, string or null")
        payload.append(
            {
                "review_id": review_id,
                "content": content,
                "rating": json_rating,
                "written_at": written_at,
            }
        )
    return payload


async def _frames(response: httpx.Response) -> AsyncIterator[bytes]:
    """개행 전 버퍼에 쌓인 데이터까지 포함해 프레임별 바이트 상한을 검증한다."""
    line = bytearray()
    lines: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        start = 0
        while start < len(chunk):
            end = chunk.find(b"\n", start)
            stop = len(chunk) if end < 0 else end + 1
            part = chunk[start:stop]
            size += len(part)
            if size > MAX_FRAME_BYTES:
                raise AnalysisStreamError("SSE frame exceeds 1 MiB")
            line.extend(part)
            start = stop
            if end < 0:
                continue
            value = bytes(line[:-1])
            if value.endswith(b"\r"):
                value = value[:-1]
            line.clear()
            if value:
                lines.append(value)
            else:
                if lines:
                    yield b"\n".join(lines)
                lines.clear()
                size = 0
    if line or lines:
        raise AnalysisStreamError("incomplete SSE frame at EOF", retryable=True)


def _json_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite JSON number")


def _event(frame: bytes, *, first_frame: bool) -> tuple[str, dict] | None:
    try:
        text = frame.decode("utf-8-sig" if first_frame else "utf-8")
        name = "message"
        data = []
        has_event = False
        for line in text.split("\n"):
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "event":
                name = value
                has_event = True
            elif field == "data":
                data.append(value)
        if not data and not has_event:
            return None
        payload = json.loads(
            "\n".join(data),
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_json_pairs,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise AnalysisStreamError("invalid SSE JSON or UTF-8") from exc
    if not isinstance(payload, dict):
        raise AnalysisStreamError("SSE data must be a JSON object")
    return name, payload


def _version(meta: dict, field: str, expected: str | None) -> str | None:
    value = meta.get(field)
    if value is not None:
        _nonempty(value, field)
    if expected is not None and value != expected:
        raise AnalysisStreamError(f"meta {field} does not match configured version")
    return value


def _same_request(data: dict, job_id: int) -> None:
    value = data.get("request_id")
    if not isinstance(value, str) or value != str(job_id):
        raise AnalysisStreamError("request_id does not match Data job")


async def _in_progress(response: httpx.Response) -> bool:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > 4096:
            return False
        body.extend(chunk)
    try:
        payload = json.loads(body, object_pairs_hook=_json_pairs)
    except (ValueError, RecursionError):
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("detail"), dict)
        and payload["detail"].get("code") == "IDEMPOTENCY_IN_PROGRESS"
    )


def _result(item: dict, ids: set[str]) -> dict:
    if not _RESULT_FIELDS.issubset(item):
        raise AnalysisStreamError("result is missing required fields")
    review_id = _nonempty(item["review_id"], "result review_id")
    if review_id not in ids:
        raise AnalysisStreamError("result review_id is outside request")
    scores = {}
    for field in _SCORES:
        number = _number(item[field], field)
        if number == -1:
            number = None
        if number is not None and not 0 <= number <= 100:
            raise AnalysisStreamError(f"{field} must be between 0 and 100, -1 or null")
        scores[field] = number
    level = item["level"]
    if level is not None and (
        not isinstance(level, str) or level not in {"safe", "warn", "danger"}
    ):
        raise AnalysisStreamError("level must be safe, warn, danger or null")
    reasons = item["reasons"]
    if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
        raise AnalysisStreamError("reasons must be a list of strings")
    return {"review_id": review_id, **scores, "level": level, "reasons": list(reasons)}


class AnalysisStreamClient:
    def __init__(
        self,
        url: str,
        token: str | None = None,
        timeout: float = 300,
        model_version: str | None = None,
        policy_version: str | None = None,
    ):
        self.url = _nonempty(url, "url")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise AnalysisStreamError("timeout must be a finite positive number")
        self.token = _header(_nonempty(token, "token"), "token") if token is not None else None
        self.timeout = timeout
        self.model_version = (
            _nonempty(model_version, "model_version") if model_version is not None else None
        )
        self.policy_version = (
            _nonempty(policy_version, "policy_version") if policy_version is not None else None
        )

    async def analyze(
        self,
        platform: str,
        product_id: str,
        reviews: list[dict],
        job_id: int,
        input_hash: str,
    ) -> AnalysisStreamResult:
        platform = _nonempty(platform, "platform")
        product_id = _nonempty(product_id, "product_id")
        if type(job_id) is not int or job_id <= 0:
            raise AnalysisStreamError("job_id must be a positive integer")
        input_hash = _header(_nonempty(input_hash, "input_hash"), "input_hash")
        payload = _reviews(reviews)
        headers = {
            "Accept": "text/event-stream",
            "X-Request-ID": str(job_id),
            "Idempotency-Key": str(job_id),
        }
        if self.token is not None:
            headers["X-Internal-Token"] = self.token
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                async with client.stream(
                    "POST",
                    self.url,
                    headers=headers,
                    json={"platform": platform, "product_id": product_id, "reviews": payload},
                ) as response:
                    if response.status_code == 409:
                        raise AnalysisStreamError(
                            "analysis HTTP status 409", retryable=await _in_progress(response)
                        )
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0]
                    if content_type.strip().lower() != "text/event-stream":
                        raise AnalysisStreamError("analysis response must be text/event-stream")
                    return await self._consume(response, payload, job_id, platform, product_id)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise AnalysisStreamError(
                f"analysis HTTP status {status}",
                retryable=status == 429 or status >= 500,
            ) from exc
        except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
            raise AnalysisStreamError("invalid analysis URL") from exc
        except httpx.HTTPError as exc:
            raise AnalysisStreamError("analysis transport failed", retryable=True) from exc

    async def _consume(
        self,
        response: httpx.Response,
        payload: list[dict],
        job_id: int,
        platform: str,
        product_id: str,
    ) -> AnalysisStreamResult:
        ids = {item["review_id"] for item in payload}
        results = {}
        meta = None
        done = False
        first_frame = True
        model_version = policy_version = ai_job_id = None
        async for frame in _frames(response):
            if done:
                raise AnalysisStreamError("frame received after done")
            event = _event(frame, first_frame=first_frame)
            first_frame = False
            if event is None:
                continue
            name, data = event
            if meta is None:
                if name != "meta":
                    raise AnalysisStreamError("first SSE event must be meta")
                _same_request(data, job_id)
                ai_job_id = _nonempty(data.get("ai_job_id"), "ai_job_id")
                count = data.get("review_count")
                if (
                    data.get("platform") != platform
                    or data.get("product_id") != product_id
                    or type(count) is not int
                    or count != len(ids)
                    or data.get("contract_version") != "v0.5"
                ):
                    raise AnalysisStreamError("meta input or contract does not match request")
                _nonempty(data.get("model_version"), "model_version")
                _nonempty(data.get("policy_version"), "policy_version")
                model_version = _version(data, "model_version", self.model_version)
                policy_version = _version(data, "policy_version", self.policy_version)
                meta = data
            elif name == "result":
                _same_request(data, job_id)
                result = _result(data, ids)
                review_id = result["review_id"]
                if review_id in results and results[review_id] != result:
                    raise AnalysisStreamError("conflicting duplicate result")
                results[review_id] = result
            elif name == "done":
                _same_request(data, job_id)
                if data.get("ai_job_id") != ai_job_id:
                    raise AnalysisStreamError("done AI job does not match meta")
                count = data.get("result_count")
                if type(count) is not int or count != len(ids) or len(results) != len(ids):
                    raise AnalysisStreamError("done count or results do not match request")
                done = True
            elif name in {"heartbeat", "progress"}:
                _same_request(data, job_id)
            elif name == "error":
                _same_request(data, job_id)
                if data.get("ai_job_id") != ai_job_id:
                    raise AnalysisStreamError("error AI job does not match meta")
                retryable = data.get("retryable", False)
                if type(retryable) is not bool:
                    raise AnalysisStreamError("error retryable must be boolean")
                raise AnalysisStreamError("analysis server sent error", retryable=retryable)
            else:
                raise AnalysisStreamError("unknown or repeated SSE event")
        if not done:
            raise AnalysisStreamError("SSE ended without done", retryable=True)
        return AnalysisStreamResult(
            [results[item["review_id"]] for item in payload],
            model_version,
            policy_version,
            ai_job_id,
        )
