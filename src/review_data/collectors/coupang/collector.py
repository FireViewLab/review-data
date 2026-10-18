"""
쿠팡(coupang.com) collector.

검색은 공식 쿠팡 파트너스 Open API(HMAC 서명)로 하고, 상세·리뷰는 Playwright로
상품 페이지를 엽니다.

2026-10-16 로컬 접근 검증에서 쿠팡 웹은 모든 경로가 Akamai로 차단됐습니다.
curl(collector 기본 UA)과 headless Chromium은 검색·상세·리뷰·robots.txt 모두 HTTP 403 Access Denied,
화면 모드(headed) Chromium은 검색 403, 상품 상세는 HTTP 200에 Access Denied 본문이었습니다.
상태 코드만으로는 차단을 판별할 수 없으므로 본문도 확인합니다.
차단은 우회하지 않습니다. 확인된 차단은 AccessBlockedError로 중단합니다.

상세·리뷰 셀렉터는 실제 페이지를 받지 못해 실페이지로 검증하지 못했습니다.
공개적으로 알려진 페이지 구조를 기준으로 한 추정이며, 아래 상수에 모아 둡니다.
"""

import hashlib
import hmac
import re
from datetime import UTC, datetime
from urllib.parse import quote

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from review_data.core.browser import BrowserCollector
from review_data.core.exceptions import AccessBlockedError, MissingCredentialError, ParseError
from review_data.core.models import Product, Review

PARTNERS_API_HOST = "https://api-gateway.coupang.com"
PARTNERS_SEARCH_PATH = "/v2/providers/affiliate_open_api/apis/openapi/products/search"
# 파트너스 검색 API가 한 번에 돌려주는 상품 수 상한.
PARTNERS_SEARCH_MAX_LIMIT = 10

PRODUCT_URL = "https://www.coupang.com/vp/products/{product_id}"
REVIEW_PATH = "/vp/product/reviews"
REVIEW_PAGE_SIZE = 10

# ── 셀렉터 (실페이지 미검증) ───────────────────
PRODUCT_NAME_SELECTOR = "h1.product-title, h2.prod-buy-header__title"
PRODUCT_PRICE_SELECTOR = ".total-price strong, .prod-sale-price .total-price"
PRODUCT_THUMBNAIL_SELECTOR = "img.prod-image__detail, .prod-image img"
PRODUCT_RATING_SELECTOR = ".prod-buy-header__info .rating-star-num, .rating-star-num"
PRODUCT_REVIEW_COUNT_SELECTOR = ".prod-buy-header__info .count"

REVIEW_ARTICLE_SELECTOR = "article.sdp-review__article__list"
REVIEW_CONTENT_SELECTOR = ".sdp-review__article__list__review__content"
REVIEW_AUTHOR_SELECTOR = ".sdp-review__article__list__info__user__name"
REVIEW_DATE_SELECTOR = ".sdp-review__article__list__info__product-info__reg-date"
REVIEW_STAR_SELECTOR = ".sdp-review__article__list__info__product-info__star-orange"
REVIEW_OPTION_SELECTOR = ".sdp-review__article__list__info__product-info__name"
REVIEW_IMAGE_SELECTOR = ".sdp-review__article__list__attachment__img"
REVIEW_HELPFUL_SELECTOR = ".sdp-review__article__list__help"

_BLOCKED_MARKER = "COUPANG_BLOCKED"
_HTTP_ERROR_MARKER = "COUPANG_HTTP_ERROR"

# 접근 거부/요청 제한과 HTTP 장애를 분리한다. 200 본문의 차단 여부는 Python에서 판별한다.
_FETCH_REVIEW_PAGE_JS = f"""
async ({{ path, productId, page, size }}) => {{
    const params = new URLSearchParams({{
        productId,
        page: String(page),
        size: String(size),
        sortBy: "ORDER_SCORE_ASC",
    }});
    const res = await fetch(path + "?" + params.toString());
    if (res.status === 403 || res.status === 429) {{
        throw new Error("{_BLOCKED_MARKER}:" + res.status);
    }}
    if (!res.ok) {{
        throw new Error("{_HTTP_ERROR_MARKER}:" + res.status);
    }}
    return await res.text();
}}
"""


def sign_partners_request(
    secret_key: str, method: str, path: str, query: str, signed_date: str
) -> str:
    """파트너스 API HMAC-SHA256 서명(hex). query 는 '?' 없이 실제 요청과 같은 바이트."""
    message = f"{signed_date}{method}{path}{query}"
    return hmac.new(secret_key.encode(), message.encode(), hashlib.sha256).hexdigest()


def build_partners_authorization(
    access_key: str,
    secret_key: str,
    method: str,
    path: str,
    query: str,
    now: datetime,
) -> str:
    """Authorization 헤더 값. now 를 주입받아 테스트에서 고정할 수 있다."""
    signed_date = now.astimezone(UTC).strftime("%y%m%dT%H%M%SZ")
    signature = sign_partners_request(secret_key, method, path, query, signed_date)
    return (
        f"CEA algorithm=HmacSHA256, access-key={access_key}, "
        f"signed-date={signed_date}, signature={signature}"
    )


def build_search_query(keyword: str, limit: int) -> str:
    return f"keyword={quote(keyword, safe='')}&limit={limit}"


def _parse_int(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _parse_float(text: str | None) -> float | None:
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def _fix_protocol(url: str | None) -> str | None:
    if not url:
        return None
    return f"https:{url}" if url.startswith("//") else url


def _is_blocked_page(html: str) -> bool:
    """Akamai Access Denied 본문인지 판별한다. 상품명에 같은 단어가 있는 것만으로는 아니다."""
    soup = BeautifulSoup(html, "lxml")
    if any(el.get_text(strip=True) for el in soup.select(PRODUCT_NAME_SELECTOR)):
        return False
    if any(title.get_text(strip=True) == "Access Denied" for title in soup.select("title")):
        return True
    if not any(h1.get_text(strip=True) == "Access Denied" for h1 in soup.select("h1")):
        return False
    # Akamai 본문은 '.'·':' 등을 HTML 엔터티로 쓰므로 디코딩된 텍스트에서 찾는다.
    text = soup.get_text(" ")
    return "errors.edgesuite.net" in text or "You don't have permission to access" in text


async def _wait_for_real_page(
    page: Page, selector: str, platform: str, response: Response | None = None
) -> None:
    """확인된 차단은 즉시 실패하고, 정상 페이지에서만 콘텐츠를 기다린다."""
    if response is not None and response.status in {403, 429}:
        raise AccessBlockedError(
            f"{platform}: 페이지 접근이 차단되었습니다 (HTTP {response.status})."
        )
    if response is not None and response.status >= 400:
        raise ParseError(f"{platform}: 페이지 요청 실패 (HTTP {response.status}).")
    if _is_blocked_page(await page.content()):
        raise AccessBlockedError(
            f"{platform}: 페이지 접근이 차단되었습니다 (Akamai Access Denied 화면)."
        )
    try:
        await page.wait_for_selector(selector)
    except PlaywrightTimeoutError as exc:
        raise ParseError(
            f"{platform}: 페이지 콘텐츠를 찾지 못했습니다 (selector: {selector})."
        ) from exc


class CoupangCollector(BrowserCollector):
    platform = "coupang"
    label = "쿠팡"
    # 파트너스 키는 검색에만 필요하다. required_settings 에 넣으면 collector 생성이
    # 실패해 상세·리뷰 경로와 목록 표시가 깨지므로 search_products 안에서 검사한다.

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        """파트너스 API 상품 검색. API 상한이 10건이고 페이지네이션이 없어 최대 10건이다."""
        access_key = self.settings.coupang_partners_access_key
        secret_key = self.settings.coupang_partners_secret_key
        if not access_key or not secret_key:
            raise MissingCredentialError(
                f"[{self.platform}] 상품 검색에는 COUPANG_PARTNERS_ACCESS_KEY와 "
                "COUPANG_PARTNERS_SECRET_KEY가 필요합니다 (.env 파일을 확인하세요)."
            )
        if limit <= 0:
            return []

        query = build_search_query(keyword, min(limit, PARTNERS_SEARCH_MAX_LIMIT))
        authorization = build_partners_authorization(
            access_key, secret_key, "GET", PARTNERS_SEARCH_PATH, query, datetime.now(UTC)
        )
        response = await self.client.get(
            f"{PARTNERS_API_HOST}{PARTNERS_SEARCH_PATH}?{query}",
            headers={"Authorization": authorization},
        )
        response.raise_for_status()

        try:
            data = response.json()
        except ValueError as exc:
            raise ParseError(f"[{self.platform}] 파트너스 API 응답이 JSON이 아닙니다.") from exc
        if not isinstance(data, dict):
            raise ParseError(f"[{self.platform}] 파트너스 API 응답 형식이 예상과 다릅니다.")
        if str(data.get("rCode")) != "0":
            raise ParseError(
                f"[{self.platform}] 파트너스 API 오류 "
                f"(rCode={data.get('rCode')}): {data.get('rMessage') or ''}"
            )
        payload = data.get("data")
        items = payload.get("productData") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ParseError(f"[{self.platform}] 파트너스 API 응답에 productData가 없습니다.")

        products: list[Product] = []
        for item in items:
            if len(products) >= limit:
                break
            if not isinstance(item, dict):
                continue
            name = (item.get("productName") or "").strip()
            product_id = item.get("productId")
            if not name or product_id in (None, ""):
                continue
            price = item.get("productPrice")
            products.append(
                Product(
                    platform=self.platform,
                    product_id=str(product_id),
                    name=name,
                    # 제휴 링크(productUrl) 대신 정규 URL을 저장한다.
                    url=PRODUCT_URL.format(product_id=product_id),
                    price=int(price) if isinstance(price, int | float) else None,
                    thumbnail_url=item.get("productImage") or None,
                    category=item.get("categoryName") or None,
                )
            )
        return products

    async def get_product(self, product_id: str) -> Product:
        url = PRODUCT_URL.format(product_id=product_id)
        async with self.page() as page:
            response = await page.goto(url, wait_until="domcontentloaded")
            await _wait_for_real_page(page, PRODUCT_NAME_SELECTOR, self.platform, response)
            content = await page.content()

        soup = BeautifulSoup(content, "lxml")
        name_el = soup.select_one(PRODUCT_NAME_SELECTOR)
        price_el = soup.select_one(PRODUCT_PRICE_SELECTOR)
        thumb_el = soup.select_one(PRODUCT_THUMBNAIL_SELECTOR)
        rating_el = soup.select_one(PRODUCT_RATING_SELECTOR)
        review_count_el = soup.select_one(PRODUCT_REVIEW_COUNT_SELECTOR)

        return Product(
            platform=self.platform,
            product_id=product_id,
            name=name_el.get_text(strip=True) if name_el else "",
            url=url,
            price=_parse_int(price_el.get_text() if price_el else None),
            thumbnail_url=_fix_protocol(thumb_el.get("src")) if thumb_el else None,
            review_count=_parse_int(review_count_el.get_text() if review_count_el else None),
            rating=_parse_float(rating_el.get_text() if rating_el else None),
        )

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        reviews: list[Review] = []
        # 빈 페이지를 만나면 바로 끝난다. 이 값은 무한 루프 방지용 안전장치다.
        max_pages = limit // REVIEW_PAGE_SIZE + 10

        async with self.page() as page:
            response = await page.goto(
                PRODUCT_URL.format(product_id=product_id), wait_until="domcontentloaded"
            )
            await _wait_for_real_page(page, PRODUCT_NAME_SELECTOR, self.platform, response)

            for page_index in range(1, max_pages + 1):
                if len(reviews) >= limit:
                    break
                try:
                    raw_html = await page.evaluate(
                        _FETCH_REVIEW_PAGE_JS,
                        {
                            "path": REVIEW_PATH,
                            "productId": product_id,
                            "page": page_index,
                            "size": REVIEW_PAGE_SIZE,
                        },
                    )
                except PlaywrightError as exc:
                    if _BLOCKED_MARKER in str(exc):
                        raise AccessBlockedError(
                            f"{self.platform}: 리뷰 요청이 차단되었습니다 "
                            "(접근 거부 또는 요청 제한)."
                        ) from exc
                    match = re.search(rf"{_HTTP_ERROR_MARKER}:(\d{{3}})", str(exc))
                    if match:
                        raise ParseError(
                            f"{self.platform}: 리뷰 요청 실패 (HTTP {match.group(1)})."
                        ) from exc
                    raise
                if not isinstance(raw_html, str):
                    raise ParseError(f"{self.platform}: 리뷰 응답 형식이 예상과 다릅니다.")
                if _is_blocked_page(raw_html):
                    raise AccessBlockedError(
                        f"{self.platform}: 리뷰 요청이 차단되었습니다 "
                        "(Akamai Access Denied 화면)."
                    )
                article_count, page_reviews = _parse_review_page(
                    raw_html, self.platform, product_id
                )
                if not article_count:
                    break
                reviews.extend(page_reviews)
                if page_index < max_pages and len(reviews) < limit:
                    await self.polite_wait()

        return reviews[:limit]


def _parse_review_page(
    raw_html: str, platform: str, product_id: str
) -> tuple[int, list[Review]]:
    """(페이지의 리뷰 블록 수, 내용이 있는 리뷰) 를 돌려준다."""
    soup = BeautifulSoup(raw_html, "lxml")
    articles = soup.select(REVIEW_ARTICLE_SELECTOR)
    reviews: list[Review] = []

    for article in articles:
        helpful_el = article.select_one(REVIEW_HELPFUL_SELECTOR)
        review_id = article.get("data-review-id") or (
            helpful_el.get("data-review-id") if helpful_el else None
        )
        content_el = article.select_one(REVIEW_CONTENT_SELECTOR)
        content = content_el.get_text(" ", strip=True) if content_el else ""
        # 내용 없이 별점만 남긴 리뷰는 표준 모델이 받지 않는다. 건너뛴다.
        if not review_id or not content:
            continue

        author_el = article.select_one(REVIEW_AUTHOR_SELECTOR)
        date_el = article.select_one(REVIEW_DATE_SELECTOR)
        star_el = article.select_one(REVIEW_STAR_SELECTOR)
        option_el = article.select_one(REVIEW_OPTION_SELECTOR)

        written_at = None
        if date_el is not None:
            try:
                written_at = datetime.strptime(date_el.get_text(strip=True), "%Y.%m.%d")
            except ValueError:
                written_at = None

        images = [
            _fix_protocol(img.get("src"))
            for img in article.select(REVIEW_IMAGE_SELECTOR)
            if img.get("src")
        ]

        reviews.append(
            Review(
                platform=platform,
                product_id=product_id,
                review_id=str(review_id),
                content=content,
                rating=_parse_float(star_el.get("data-rating")) if star_el else None,
                author=author_el.get_text(strip=True) if author_el else None,
                written_at=written_at,
                option=option_el.get_text(strip=True) if option_el else None,
                images=[url for url in images if url],
                helpful_count=_parse_int(helpful_el.get("data-count")) if helpful_el else None,
            )
        )

    return len(articles), reviews
