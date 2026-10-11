"""수집 과정에서 발생하는 예외 정의."""


class CollectorError(Exception):
    """수집 관련 예외의 최상위 클래스."""


class NotSupportedError(CollectorError):
    """해당 플랫폼이 이 기능을 지원하지 않을 때 사용합니다.

    예) 공개 검색 API가 없어서 search_products 를 제공할 수 없는 경우.

    실제 버그(AttributeError, TypeError 등)와 구분하기 위해
    '의도적으로 지원하지 않음'은 반드시 이 예외를 사용하세요.
    """


class MissingCredentialError(CollectorError):
    """collector 가 필요로 하는 인증 정보가 설정되지 않았을 때."""


class ParseError(CollectorError):
    """응답 구조가 예상과 달라 파싱에 실패했을 때.

    사이트 개편으로 셀렉터나 JSON 구조가 바뀌면 이 예외가 발생합니다.
    """


class AccessBlockedError(ParseError):
    """정상 접근에서 차단·로그인·봇 확인이 관측되어 수집을 중단했을 때."""
