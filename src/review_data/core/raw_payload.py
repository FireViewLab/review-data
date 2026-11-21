"""플랫폼 원본 리뷰 응답을 저장 가능한 형태로 정리한다.

원본에는 구매 인증·재구매·체험단 여부처럼 표준 필드에 없는 정보가 들어 있다. 학습·검수에
쓰려고 남기되, 작성자를 다시 찾을 수 있는 값(닉네임·작성자 ID·회원 번호·프로필)은 저장하지
않는다. 작성자 반복은 author 컬럼과 별도 해시로만 본다.
"""

import json
import re
from typing import Any

# 키 이름에 이 조각이 들어가면 값 전체(하위 객체 포함)를 버린다.
_IDENTITY_KEY = re.compile(
    r"nick|writer|reviewer|owner|member|profile|user|author|email|phone|mobile|birth|address|^ip$|account|order_?(no|id|num)",
    re.IGNORECASE,
)

# 저장 상한. 이보다 크면 비정상 응답으로 보고 남기지 않는다.
MAX_BYTES = 32_000


def _clean(value: Any, content: str) -> Any:
    if isinstance(value, dict):
        return {
            k: _clean(v, content)
            for k, v in value.items()
            if not _IDENTITY_KEY.search(str(k))
            and not (isinstance(v, str) and v.strip() == content)
        }
    if isinstance(value, list):
        return [
            _clean(v, content) for v in value if not (isinstance(v, str) and v.strip() == content)
        ]
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


def sanitize(payload: Any, content: str) -> dict | None:
    """식별 키와 본문 중복을 뺀 원본을 돌려준다. 저장할 수 없으면 None."""
    if not isinstance(payload, dict):
        return None
    cleaned = _clean(payload, content.strip())
    if not cleaned:
        return None
    try:
        size = len(json.dumps(cleaned, ensure_ascii=False, default=str).encode())
    except (TypeError, ValueError):
        return None
    return cleaned if size <= MAX_BYTES else None
