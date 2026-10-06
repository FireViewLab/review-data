"""
모든 collector 의 기반 클래스.

팀원은 self.client (httpx.AsyncClient) 를 그냥 쓰기만 하면 됩니다.
클라이언트 생성/정리는 async with 블록에서 자동으로 처리됩니다.

    async with MyCollector() as collector:
        products = await collector.search_products("텀블러")
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Self

import httpx

from review_data.core.exceptions import MissingCredentialError, NotSupportedError
from review_data.core.models import Product, Review
from review_data.core.settings import Settings, get_settings

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}


class BaseCollector(ABC):
    """HTTP 요청(오픈 API / 내부 JSON / 정적 HTML)으로 수집하는 경우 상속.

    JS 렌더링이 필요하면 BrowserCollector 를 상속하세요.
    """

    platform: str
    """플랫폼 식별자. collector 폴더명과 반드시 동일하게 맞추세요."""

    label: str = ""
    """CLI 목록에 표시될 한글 이름. 비우면 platform 이 사용됩니다."""

    required_settings: tuple[str, ...] = ()
    """이 collector 가 반드시 필요로 하는 설정값 이름 (Settings 의 속성명).
    예: ("elevenst_api_key",)
    없으면 collector 생성 시점에 MissingCredentialError 가 발생합니다."""

    def __init__(self, settings: Settings | None = None) -> None:
        # settings 를 주입받을 수 있게 해두면 테스트와 본 서버 이식이 쉬워집니다.
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None
        self._check_credentials()

    def _check_credentials(self) -> None:
        missing = [
            name for name in self.required_settings if not getattr(self.settings, name, None)
        ]
        if missing:
            keys = ", ".join(name.upper() for name in missing)
            raise MissingCredentialError(
                f"[{self.platform}] 다음 설정이 필요합니다: {keys} (.env 파일을 확인하세요)"
            )

    # ── 리소스 생명주기 (팀원이 신경 쓸 필요 없음) ──────────
    async def __aenter__(self) -> Self:
        if self._client is not None:
            raise RuntimeError(f"[{self.platform}] collector 는 이미 사용 중입니다.")
        self._client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=self.settings.request_timeout,
            follow_redirects=True,
        )
        try:
            await self.setup()
        except BaseException as exc:
            # __aenter__ 실패 시 Python 은 __aexit__ 을 호출하지 않는다.
            await self.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        try:
            try:
                await self.teardown()
            finally:
                client, self._client = self._client, None
                if client is not None:
                    await client.aclose()
        except Exception:
            # 정리 오류 때문에 수집 오류나 취소의 원인이 사라지지 않게 한다.
            if not exc_info or exc_info[0] is None:
                raise

    async def setup(self) -> None:  # noqa: B027 - 선택적 리소스 훅
        """추가 준비가 필요하면 오버라이드하세요 (선택)."""

    async def teardown(self) -> None:  # noqa: B027 - 선택적 리소스 훅
        """추가 정리가 필요하면 오버라이드하세요 (선택)."""

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                f"[{self.platform}] 'async with' 블록 안에서만 client 를 사용할 수 있습니다."
            )
        return self._client

    async def polite_wait(self) -> None:
        """여러 페이지를 순회할 때 요청 사이에 호출하세요. 서버 부담을 줄입니다."""
        await asyncio.sleep(self.settings.request_delay)

    # ── 구현 대상 ────────────────────────────────────
    # 지원하지 않는 기능은 NotSupportedError 를 raise 하세요.
    @abstractmethod
    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        """키워드로 상품을 검색합니다."""
        raise NotSupportedError(f"{self.platform}: 상품 검색을 지원하지 않습니다.")

    @abstractmethod
    async def get_product(self, product_id: str) -> Product:
        """상품 상세 정보를 조회합니다."""
        raise NotSupportedError(f"{self.platform}: 상품 상세 조회를 지원하지 않습니다.")

    @abstractmethod
    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        """상품의 리뷰를 수집합니다."""
        raise NotSupportedError(f"{self.platform}: 리뷰 수집을 지원하지 않습니다.")

    # ── 선택 구현 ────────────────────────────────────
    async def related_products(self, product_id: str, limit: int = 20) -> list[str]:
        """같은 판매처의 다른 상품 ID 를 돌려줍니다 (시드를 넓힐 때 사용).

        검색 수단이 없는 플랫폼은 상품 하나를 알아야 다른 상품을 찾을 수 있습니다.
        그런 플랫폼만 구현하면 됩니다.
        """
        raise NotSupportedError(f"{self.platform}: 관련 상품 조회를 지원하지 않습니다.")

    async def iter_reviews(
        self, product_id: str, limit: int = 50
    ) -> AsyncIterator[Review]:
        """리뷰를 수집되는 대로 하나씩 내보냅니다 (SSE 스트림이 사용).

        구현하지 않아도 됩니다. 기본 동작은 get_reviews 를 그대로 부른 뒤 결과를
        하나씩 흘려보내는 것이라, 기존 collector 는 아무것도 바꾸지 않아도 스트림이
        동작합니다. 다만 이 경우 리뷰는 수집이 다 끝난 뒤에야 한꺼번에 나갑니다.

        페이지를 넘겨가며 수집하는 collector 는 이 메서드를 오버라이드해서 한 페이지를
        받을 때마다 yield 하세요. 그래야 수집이 오래 걸릴 때 앞쪽 리뷰부터 분석 서버로
        흘려보낼 수 있습니다.

            async def iter_reviews(self, product_id, limit=50):
                collected = 0
                for page in range(1, 10):
                    for review in await self._fetch_page(product_id, page):
                        if collected >= limit:
                            return
                        yield review
                        collected += 1
                    await self.polite_wait()
        """
        for review in await self.get_reviews(product_id, limit=limit):
            yield review
