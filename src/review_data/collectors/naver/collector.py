"""네이버 브랜드스토어 상품 및 리뷰 collector.

네이버 쇼핑 검색 API 는 2026-07-31 종료됐고 대체 API 가 없다. 상품·리뷰는 브랜드스토어
상품 페이지를 브라우저로 열고, 페이지가 스스로 보낸 요청의 응답(JSON)만 읽는다.
내부 API 를 직접 호출하거나 헤더·쿠키를 복제하지 않는다.

- 스마트스토어(smartstore.naver.com)는 새 브라우저에서 첫 요청부터 캡차가 떠서 지원하지 않는다.
- 리뷰는 리뷰 탭 첫 페이지(랭킹순, 최대 20건)만 받는다. 2026-10 화면에는 정렬 버튼이 없고
  스크롤·다음 버튼으로 2페이지 요청이 나가지 않는다.
- 캡차·로그인·차단 응답이 보이면 바로 실패로 끝낸다. 재시도나 우회는 하지 않는다.
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from playwright.async_api import Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from review_data.core.browser import BrowserCollector
from review_data.core.exceptions import NotSupportedError, ParseError
from review_data.core.models import Product, Review
from review_data.core.settings import Settings, get_settings

PRODUCT_URL = "https://brand.naver.com/{store}/products/{product_no}"
PRODUCT_ID_PATTERN = re.compile(r"^(?P<store>[A-Za-z0-9_-]+):(?P<product_no>\d+)$")
PRODUCT_URL_PATTERN = re.compile(
    r"https?://(?P<host>brand|smartstore)\.naver\.com/"
    r"(?P<store>[^/?#]+)/products/(?P<product_no>\d+)"
)
REVIEW_PAGE_PATH = "/contents/reviews/query-pages"
REVIEW_TAB_TEXT = re.compile(r"^리뷰\s*[\d,]+$")
# 상품 페이지가 스스로 받아 오는 "같은 스토어의 다른 상품" 목록들.
RELATED_PATHS = ("/simple-products", "/recommends/keep-cart", "/product-other-recommend/")
RELATED_SCROLL_STEPS = 6

BLOCKED_STATUSES = {418, 429, 490}
BLOCK_MARKERS = ("captcha", "보안 확인", "접속이 일시적으로 제한")
RESPONSE_TIMEOUT_SECONDS = 15.0


def parse_product_id(product_id: str) -> tuple[str, str]:
    """`{스토어}:{상품번호}` 를 (스토어, 상품번호) 로 나눈다.

    브랜드스토어 URL 은 스토어 이름이 있어야 열린다. 상품 URL 을 그대로 받으면 DB 키가
    입력값과 어긋나므로 받지 않고, 올바른 형식을 알려준다.
    """
    match = PRODUCT_ID_PATTERN.match(product_id.strip())
    if match:
        return match["store"], match["product_no"]

    url_match = PRODUCT_URL_PATTERN.search(product_id)
    if url_match and url_match["host"] == "smartstore":
        raise NotSupportedError(
            "[naver] 스마트스토어 상품은 지원하지 않습니다 (브랜드스토어만 수집 가능)."
        )
    if url_match:
        raise ParseError(
            f"[naver] 상품 URL 대신 '{url_match['store']}:{url_match['product_no']}' "
            "형식으로 입력하세요."
        )
    raise ParseError(
        f"[naver] 상품 ID 형식이 올바르지 않습니다: {product_id!r} "
        "(예: zinus:6000252751)"
    )


def is_blocked(status: int | None, html: str, url: str) -> bool:
    if status in BLOCKED_STATUSES:
        return True
    if "nid.naver.com" in url:  # 로그인 페이지로 넘어감
        return True
    lowered = html.lower()
    return any(marker in lowered for marker in BLOCK_MARKERS)


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_product(data: dict, product_id: str) -> Product:
    store, product_no = parse_product_id(product_id)
    name = data.get("name")
    if not name:
        raise ParseError(f"[naver] 상품 정보에 이름이 없습니다: {product_id}")

    search_info = data.get("naverShoppingSearchInfo") or {}
    review_amount = data.get("reviewAmount") or {}
    price = (data.get("benefitsView") or {}).get("discountedSalePrice") or data.get("salePrice")
    return Product(
        platform="naver",
        product_id=product_id,
        name=name,
        url=PRODUCT_URL.format(store=store, product_no=product_no),
        brand=search_info.get("brandName"),
        manufacturer=search_info.get("manufacturerName"),
        seller=(data.get("channel") or {}).get("channelName"),
        price=_to_int(price),
        thumbnail_url=(data.get("representImage") or {}).get("url"),
        category=(data.get("category") or {}).get("wholeCategoryName"),
        review_count=_to_int(review_amount.get("totalReviewCount")),
        rating=_to_float(review_amount.get("averageReviewScore")),
    )


def parse_reviews(data: dict, product_id: str, limit: int) -> list[Review]:
    contents = data.get("contents")
    if not isinstance(contents, list):
        raise ParseError("[naver] 리뷰 응답 형식이 예상과 다릅니다.")

    reviews: list[Review] = []
    for item in contents:
        content = (item.get("reviewContent") or "").strip()
        review_id = item.get("id")
        # 내용이 없는 리뷰(별점만)는 표준 모델 제약상 저장할 수 없다.
        if not content or review_id is None:
            continue
        created = item.get("createDate")
        reviews.append(
            Review(
                platform="naver",
                product_id=product_id,
                review_id=str(review_id),
                content=content,
                rating=_to_float(item.get("reviewScore")),
                # 작성자 식별값(maskedWriterId, writerId 등)은 개인정보라 저장하지 않는다.
                author=None,
                written_at=datetime.fromisoformat(created) if created else None,
                option=item.get("productOptionContent") or None,
                images=[
                    attach["attachUrl"]
                    for attach in item.get("reviewAttaches") or []
                    if attach.get("reviewAttachmentType") == "I" and attach.get("attachUrl")
                ],
                helpful_count=_to_int(item.get("helpCount")),
            )
        )
        if len(reviews) >= limit:
            break
    return reviews


def parse_related(
    payloads: list[Any], *, channel_uid: str, store: str, exclude_product_no: str, limit: int
) -> list[str]:
    """추천·인기 상품 응답들에서 같은 스토어의 판매 중인 상품 식별자를 뽑는다.

    목록 항목에는 스토어 이름이 없고 스토어 고유 ID(channelUid)만 있다. 방문한 상품과
    ID 가 같은 것만 골라야 식별자({스토어}:{상품번호})를 만들 수 있다.
    """
    found: dict[str, None] = {}

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            channel = node.get("channel")
            product_no = node.get("id")
            if (
                isinstance(channel, dict)
                and channel.get("channelUid") == channel_uid
                and product_no is not None
                and str(product_no) != exclude_product_no
                and node.get("productStatusType", "SALE") == "SALE"
            ):
                found.setdefault(f"{store}:{product_no}")
            for value in node.values():
                walk(value)

    for payload in payloads:
        walk(payload)
    return list(found)[:limit]


@dataclass
class _PageCapture:
    """상품 페이지 한 번 방문에서 받은 응답들."""

    product: dict | None = None
    reviews: dict | None = None
    related: list[Any] = field(default_factory=list)
    product_ready: asyncio.Event = field(default_factory=asyncio.Event)
    reviews_ready: asyncio.Event = field(default_factory=asyncio.Event)


class NaverCollector(BrowserCollector):
    platform = "naver"
    label = "네이버 브랜드스토어"

    def __init__(self, settings: Settings | None = None) -> None:
        # 오늘의집과 같은 이유로 화면 모드로 띄운다. 전역 설정은 바꾸지 않는다.
        source_settings = settings or get_settings()
        super().__init__(
            settings=source_settings.model_copy(update={"headless": False}, deep=True)
        )
        # 워커와 CLI 는 한 collector 로 상품과 리뷰를 차례로 부른다. 한 번 방문한 결과를
        # 재사용해 job 당 페이지 방문을 1회로 줄인다.
        self._captures: dict[str, _PageCapture] = {}

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        raise NotSupportedError(
            "[naver] 상품 검색을 지원하지 않습니다 (네이버 쇼핑 검색 API 종료, 대체 수단 없음)."
        )

    async def get_product(self, product_id: str) -> Product:
        capture = await self._visit(product_id)
        if capture.product is None:
            raise ParseError(f"[naver] 상품 정보를 받지 못했습니다: {product_id}")
        return parse_product(capture.product, product_id)

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        if limit <= 0:
            return []
        capture = await self._visit(product_id)
        if capture.reviews is None:
            total = ((capture.product or {}).get("reviewAmount") or {}).get("totalReviewCount")
            if total == 0:
                return []
            raise ParseError(f"[naver] 리뷰 목록을 받지 못했습니다: {product_id}")
        return parse_reviews(capture.reviews, product_id, limit)

    async def related_products(self, product_id: str, limit: int = 20) -> list[str]:
        if limit <= 0:
            return []
        capture = await self._visit(product_id, with_related=True)
        if capture.product is None:
            raise ParseError(f"[naver] 상품 정보를 받지 못했습니다: {product_id}")
        store, product_no = parse_product_id(product_id)
        channel_uid = (capture.product.get("channel") or {}).get("channelUid")
        if not channel_uid:
            raise ParseError(f"[naver] 스토어 정보를 찾지 못했습니다: {product_id}")
        return parse_related(
            capture.related,
            channel_uid=channel_uid,
            store=store,
            exclude_product_no=product_no,
            limit=limit,
        )

    async def _visit(self, product_id: str, *, with_related: bool = False) -> _PageCapture:
        cached = self._captures.get(product_id)
        # 관련 상품 목록은 화면을 내려야 요청된다. 평소 수집에서는 내리지 않으므로,
        # 관련 상품이 필요한데 받아 둔 게 없으면 다시 방문한다.
        if cached is not None and (cached.related or not with_related):
            return cached

        store, product_no = parse_product_id(product_id)
        url = PRODUCT_URL.format(store=store, product_no=product_no)
        product_path = re.compile(rf"/v2/channels/[^/]+/products/{product_no}(\?|$)")
        capture = _PageCapture()

        async def on_response(response: Response) -> None:
            if not response.ok:
                return
            if capture.product is None and product_path.search(response.url):
                capture.product = await response.json()
                capture.product_ready.set()
            elif capture.reviews is None and response.url.endswith(REVIEW_PAGE_PATH):
                capture.reviews = await response.json()
                capture.reviews_ready.set()
            elif any(path in response.url for path in RELATED_PATHS):
                capture.related.append(await response.json())

        async with self.page() as page:
            page.on("response", on_response)
            await self._open(page, url)
            await self._wait(capture.product_ready, "상품 정보")
            if capture.reviews is None:
                await self._open_review_tab(page)
            # 리뷰가 0건인 상품은 리뷰 요청이 오지 않을 수 있어, 여기서는 실패로 보지 않는다.
            try:
                await asyncio.wait_for(capture.reviews_ready.wait(), RESPONSE_TIMEOUT_SECONDS)
            except TimeoutError:
                pass
            if with_related:
                for _ in range(RELATED_SCROLL_STEPS):
                    await page.mouse.wheel(0, 1000)
                    await page.wait_for_timeout(900)

        self._captures[product_id] = capture
        return capture

    async def _open(self, page: Page, url: str) -> None:
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError as exc:
            raise ParseError(f"[naver] 페이지 로딩 시간이 초과됐습니다: {url}") from exc

        status = response.status if response is not None else None
        if is_blocked(status, await page.content(), page.url):
            raise ParseError(
                f"[naver] 접근이 차단됐습니다 (캡차·로그인·HTTP {status}). "
                "우회하지 않고 중단합니다."
            )
        if status is not None and status >= 400:
            raise ParseError(f"[naver] 페이지 요청 실패: HTTP {status} ({url})")

    async def _open_review_tab(self, page: Page) -> None:
        tab = page.get_by_text(REVIEW_TAB_TEXT).first
        if not await tab.count():
            return
        await tab.scroll_into_view_if_needed()
        # 탭 위에 고정 헤더가 겹쳐 일반 클릭이 막힌다. 사람이 누르는 것과 같은 클릭 이벤트다.
        await tab.click(force=True)

    @staticmethod
    async def _wait(event: asyncio.Event, what: str) -> None:
        try:
            await asyncio.wait_for(event.wait(), RESPONSE_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise ParseError(f"[naver] {what} 응답을 받지 못했습니다.") from exc
