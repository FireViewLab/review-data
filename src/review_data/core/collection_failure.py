"""수집 실패의 안전한 분류와 정기 재예약 대기 정책."""

import httpx

from review_data.core.exceptions import (
    AccessBlockedError,
    MissingCredentialError,
    NotSupportedError,
    ParseError,
    ProductNotFoundError,
)

PRIORITY = {
    kind: n
    for n, kind in enumerate(
        (
            "unknown",
            "parse",
            "network",
            "timeout",
            "storage",
            "not_found",
            "blocked",
            "unsupported",
            "credentials",
        )
    )
}


def failure_kind(error: Exception | str) -> str:
    if isinstance(error, MissingCredentialError):
        return "credentials"
    if isinstance(error, NotSupportedError):
        return "unsupported"
    if isinstance(error, AccessBlockedError):
        return "blocked"
    if isinstance(error, ProductNotFoundError):
        return "not_found"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, httpx.HTTPStatusError):
        # 403·429는 사이트가 요청을 막거나 줄이라는 신호다. 원인 미상으로 두면 짧게 쉬고
        # 다시 두드려 차단을 길게 만든다.
        status = error.response.status_code
        if status in (403, 429):
            return "blocked"
        if status in (404, 410):
            return "not_found"
        if status >= 500:
            return "network"
    # 기존 collector/이력은 문자열 오류만 남기므로 명확한 신호만 보완한다.
    message = (
        f"{type(error).__name__}: {error}" if isinstance(error, Exception) else error
    ).lower()
    if any(
        x in message
        for x in (
            "차단",
            "captcha",
            "cloudflare",
            "http 403",
            "http 429",
            "403 forbidden",
            "429 too many requests",
        )
    ):
        return "blocked"
    if "저장 실패" in message:
        return "storage"
    if any(x in message for x in ("시간 초과", "시간을 초과", "timeout", "timed out")):
        return "timeout"
    if any(x in message for x in ("http 404", "http 410")):
        return "not_found"
    if any(
        x in message
        for x in (
            "connecterror",
            "connection",
            "network",
            "readerror",
            "remoteprotocolerror",
            "http 50",
        )
    ):
        return "network"
    if isinstance(error, ParseError):
        return "parse"
    return "unknown"


def dominant_failure(kinds: list[str]) -> str:
    return max(kinds, key=lambda kind: PRIORITY.get(kind, 0), default="unknown")


def retry_delay(kind: str, consecutive: int, base_seconds: int = 21600) -> int:
    """최대 하루. 0 설정은 대기를 끄며 성공 후 실패 횟수는 초기화된다."""
    if base_seconds <= 0:
        return 0
    initial = {
        "blocked": base_seconds,
        "credentials": 86400,
        "unsupported": 86400,
        "not_found": 86400,
        "timeout": max(60, base_seconds // 36),
        "network": max(60, base_seconds // 36),
        "storage": max(60, base_seconds // 36),
    }.get(kind, max(60, base_seconds // 6))
    return min(86400, initial * 2 ** min(max(consecutive - 1, 0), 8))
