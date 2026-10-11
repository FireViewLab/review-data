"""G마켓 collector.

G마켓의 검색·상세·상품평 경로는 Cloudflare 보호 대상이므로 브라우저
컨텍스트를 사용합니다. 스텔스 패치나 프록시 로테이션 같은 우회는 하지 않으며,
정상적인 Playwright 요청이 차단되면 원인이 드러나는 ParseError로 실패합니다.

- 검색  : www.gmarket.co.kr/n/search 페이지의 상품 카드
- 상세  : item.gmarket.co.kr 상품 페이지의 JSON-LD(및 HTML fallback)
- 상품평: POST /Review 셀 + POST /Review/Text 후속 페이지
"""

import hashlib
import json
import re
from datetime import datetime
from urllib.parse import quote

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from review_data.core.browser import BrowserCollector
from review_data.core.exceptions import AccessBlockedError, ParseError
from review_data.core.models import Product, Review

SEARCH_URL = "https://www.gmarket.co.kr/n/search"
PRODUCT_URL = "https://item.gmarket.co.kr/Item?goodsCode={product_id}"
REVIEW_SHELL_PATH = "/Review"
REVIEW_TEXT_PATH = "/Review/Text"

REVIEW_PAGE_SIZE = 10

_GOODS_CODE_RE = re.compile(r"[?&]goodscode=(\d+)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_DATE_RE = re.compile(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})")
_BLOCK_MARKERS = (
    "cf-chl-",
    "cf_chl_",
    "잠시만 기다리십시오",
    "봇 확인 절차",
    "cf-mitigated",
)
_SEARCH_EMPTY_MARKERS = ("검색결과가 없습니다", "검색 결과가 없습니다")
_REVIEW_EMPTY_MARKERS = (
    "등록된 상품평이 없습니다",
    "상품평이 없습니다",
    "등록된 리뷰가 없습니다",
)

_FETCH_REVIEW_HTML_JS = """
async ({ path, body }) => {
    const response = await fetch(path, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
    });
    return {
        status: response.status,
        contentType: response.headers.get("content-type") || "",
        text: await response.text(),
    };
}
"""


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    match = _NUMBER_RE.search(str(value))
    return float(match.group()) if match else None


def _absolute_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    return f"https:{url}" if url.startswith("//") else url


def _parse_date(value: str) -> datetime | None:
    match = _DATE_RE.search(value)
    if not match:
        return None
    try:
        return datetime(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _is_blocked(status: int | None, html: str) -> bool:
    if status in {403, 429}:
        return True
    lowered = html.lower()
    return any(marker.lower() in lowered for marker in _BLOCK_MARKERS)


def _has_empty_marker(html: str, markers: tuple[str, ...]) -> bool:
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    return any(marker in text for marker in markers)


def _extract_product_ld_json(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    candidates: list[object] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates.extend(data if isinstance(data, list) else [data])

    while candidates:
        candidate = candidates.pop(0)
        if not isinstance(candidate, dict):
            continue
        if candidate.get("@type") == "Product":
            return candidate
        graph = candidate.get("@graph")
        if isinstance(graph, list):
            candidates.extend(graph)
    return {}


def _review_id(product_id: str, *parts: object) -> str:
    raw = "\x1f".join([product_id, *(str(part or "").strip() for part in parts)])
    digest = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"gm-{digest}"


class GmarketCollector(BrowserCollector):
    platform = "gmarket"
    label = "G마켓"

    async def _load_page(self, page: Page, url: str, selector: str | None = None) -> str:
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError as exc:
            raise ParseError(f"{self.platform}: 페이지 응답 시간을 초과했습니다.") from exc

        html = await page.content()
        self._raise_if_blocked(response, html)

        if selector:
            try:
                await page.wait_for_selector(selector, timeout=5_000)
            except PlaywrightTimeoutError:
                # 검색 결과가 없거나 정적 HTML에 JSON-LD만 있을 수 있어
                # 셀렉터 타임아웃 자체는 실패로 간주하지 않는다.
                pass
            html = await page.content()
            self._raise_if_blocked(response, html)
        return html

    def _raise_if_blocked(self, response: Response | None, html: str) -> None:
        status = response.status if response is not None else None
        if _is_blocked(status, html):
            status_text = f"HTTP {status}" if status is not None else "응답 없음"
            raise AccessBlockedError(
                f"{self.platform}: G마켓이 자동화 브라우저 요청을 차단했습니다 "
                f"(Cloudflare 봇 확인, {status_text})."
            )
        if status is not None and status >= 400:
            raise ParseError(f"{self.platform}: 페이지 요청 실패 (HTTP {status}).")

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        if not keyword or limit <= 0:
            return []

        async with self.page() as page:
            url = f"{SEARCH_URL}?keyword={quote(keyword)}"
            html = await self._load_page(page, url, ".box__component")

        soup = BeautifulSoup(html, "lxml")
        cards = soup.select(".box__component")
        if not cards:
            if _has_empty_marker(html, _SEARCH_EMPTY_MARKERS):
                return []
            raise ParseError(f"{self.platform}: 검색 페이지에서 상품 카드를 찾지 못했습니다.")

        products: list[Product] = []
        seen: set[str] = set()
        for card in cards:
            product = self._parse_search_card(card)
            if product is None or product.product_id in seen:
                continue
            seen.add(product.product_id)
            products.append(product)
            if len(products) >= limit:
                break
        if not products:
            if _has_empty_marker(html, _SEARCH_EMPTY_MARKERS):
                return []
            raise ParseError(f"{self.platform}: 검색 상품 카드의 구조가 예상과 다릅니다.")
        return products

    def _parse_search_card(self, card) -> Product | None:
        link = card.select_one(
            ".box__item-title a[href*='goodsCode'], "
            ".box__item-title a[href*='goodscode'], "
            "a[href*='goodsCode'], a[href*='goodscode']"
        )
        if link is None:
            return None
        href = link.get("href", "")
        match = _GOODS_CODE_RE.search(href)
        if not match:
            return None
        product_id = match.group(1)

        name_el = card.select_one(".text__item")
        name = name_el.get_text(" ", strip=True) if name_el else link.get_text(" ", strip=True)
        if not name:
            return None

        price_el = card.select_one(".box__price-seller strong, .s-price strong")
        image_el = card.select_one(".box__image img")
        seller_el = card.select_one(".box__seller .text, .text__seller")
        brand_el = card.select_one(".text__brand")
        review_count_el = card.select_one(
            ".list-item__feedback-count .text, .list-item__review-count .text"
        )
        rating_el = card.select_one(".list-item__score .for-a11y, .image__awards-points")

        thumbnail = None
        if image_el is not None:
            thumbnail = _absolute_url(
                image_el.get("src") or image_el.get("data-src") or image_el.get("data-original")
            )

        return Product(
            platform=self.platform,
            product_id=product_id,
            name=name,
            url=PRODUCT_URL.format(product_id=product_id),
            brand=brand_el.get_text(" ", strip=True) if brand_el else None,
            seller=seller_el.get_text(" ", strip=True) if seller_el else None,
            price=_to_int(price_el.get_text() if price_el else None),
            thumbnail_url=thumbnail,
            review_count=_to_int(review_count_el.get_text() if review_count_el else None),
            rating=_to_float(rating_el.get_text() if rating_el else None),
        )

    async def get_product(self, product_id: str) -> Product:
        url = PRODUCT_URL.format(product_id=product_id)
        async with self.page() as page:
            html = await self._load_page(page, url, "h1")
        return self._parse_product_page(product_id, html)

    def _parse_product_page(self, product_id: str, html: str) -> Product:
        soup = BeautifulSoup(html, "lxml")
        data = _extract_product_ld_json(html)

        offers = data.get("offers") or {}
        if isinstance(offers, list):
            offers = next((item for item in offers if isinstance(item, dict)), {})
        brand_data = data.get("brand") or {}
        seller_data = data.get("seller") or {}
        aggregate = data.get("aggregateRating") or {}

        name = str(data.get("name") or "").strip()
        if not name:
            name_el = soup.select_one("h1.itemtit, h1.item-title, h1")
            name = name_el.get_text(" ", strip=True) if name_el else ""
        if not name:
            og_title = soup.find("meta", property="og:title")
            name = og_title.get("content", "").strip() if og_title else ""
        if not name:
            raise ParseError(
                f"{self.platform}: 상품 {product_id} 페이지에서 상품명을 찾지 못했습니다."
            )

        image = data.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url") or image.get("contentUrl")
        if not image:
            og_image = soup.find("meta", property="og:image")
            image = og_image.get("content") if og_image else None

        price = offers.get("price") if isinstance(offers, dict) else None
        if price is None:
            price_el = soup.select_one(".price_real, .box__price-seller strong")
            price = price_el.get_text() if price_el else None

        brand = brand_data.get("name") if isinstance(brand_data, dict) else brand_data
        if not brand:
            brand_el = soup.select_one(".text__brand")
            brand = brand_el.get_text(" ", strip=True) if brand_el else None

        seller = seller_data.get("name") if isinstance(seller_data, dict) else seller_data
        if not seller:
            seller_el = soup.select_one("a.link__seller, .shopurl strong")
            seller = seller_el.get_text(" ", strip=True) if seller_el else None

        category = data.get("category")
        if not category:
            category_parts = [
                element.get_text(" ", strip=True)
                for element in soup.select(".location-navi a.on, .location a.now")
            ]
            category = " > ".join(filter(None, category_parts)) or None

        return Product(
            platform=self.platform,
            product_id=product_id,
            name=name,
            url=PRODUCT_URL.format(product_id=product_id),
            brand=str(brand).strip() if brand else None,
            seller=str(seller).strip() if seller else None,
            price=_to_int(price),
            thumbnail_url=_absolute_url(image),
            category=str(category).strip() if category else None,
            review_count=(
                _to_int(aggregate.get("reviewCount")) if isinstance(aggregate, dict) else None
            ),
            rating=(
                _to_float(aggregate.get("ratingValue")) if isinstance(aggregate, dict) else None
            ),
        )

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        if limit <= 0:
            return []

        reviews: list[Review] = []
        seen: set[str] = set()
        url = PRODUCT_URL.format(product_id=product_id)

        async with self.page() as page:
            await self._load_page(page, url, "h1")

            shell_html = await self._fetch_review_html(
                page,
                REVIEW_SHELL_PATH,
                self._review_body(product_id, page_number=1),
            )
            total_pages = self._parse_total_pages(shell_html)
            first_page = self._parse_review_page(product_id, shell_html)
            if not first_page:
                if total_pages == 0 or _has_empty_marker(shell_html, _REVIEW_EMPTY_MARKERS):
                    return []
                raise ParseError(f"{self.platform}: 상품평 응답에서 상품평 행을 찾지 못했습니다.")
            self._append_unique(reviews, seen, first_page, limit)

            if total_pages is None and len(first_page) >= REVIEW_PAGE_SIZE and len(reviews) < limit:
                raise ParseError(
                    f"{self.platform}: 상품평 응답에서 전체 페이지 수를 찾지 못했습니다."
                )

            for page_number in range(2, (total_pages or 1) + 1):
                if len(reviews) >= limit:
                    break
                await self.polite_wait()
                page_html = await self._fetch_review_html(
                    page,
                    REVIEW_TEXT_PATH,
                    self._review_body(product_id, page_number, total_pages),
                )
                page_reviews = self._parse_review_page(product_id, page_html)
                if not page_reviews:
                    break
                added = self._append_unique(reviews, seen, page_reviews, limit)
                if added == 0:
                    break

        return reviews

    async def _fetch_review_html(self, page: Page, path: str, body: str) -> str:
        try:
            result = await page.evaluate(_FETCH_REVIEW_HTML_JS, {"path": path, "body": body})
        except PlaywrightError as exc:
            raise ParseError(f"{self.platform}: 상품평 요청을 완료하지 못했습니다.") from exc

        if not isinstance(result, dict):
            raise ParseError(f"{self.platform}: 상품평 응답 형식이 예상과 다릅니다.")
        status = result.get("status")
        html = result.get("text")
        if not isinstance(status, int) or not isinstance(html, str):
            raise ParseError(f"{self.platform}: 상품평 응답 형식이 예상과 다릅니다.")
        if _is_blocked(status, html):
            raise AccessBlockedError(
                f"{self.platform}: G마켓이 상품평 요청을 차단했습니다 "
                f"(Cloudflare 봇 확인, HTTP {status})."
            )
        if status >= 400:
            raise ParseError(f"{self.platform}: 상품평 요청이 HTTP {status}로 실패했습니다.")
        return html

    @staticmethod
    def _review_body(product_id: str, page_number: int, total_pages: int | None = None) -> str:
        fields = [f"goodsCode={quote(product_id)}", f"pageNo={page_number}"]
        if total_pages is not None:
            fields.append(f"totalPage={total_pages}")
        return "&".join(fields)

    @staticmethod
    def _parse_total_pages(html: str) -> int | None:
        soup = BeautifulSoup(html, "lxml")
        pagination = soup.select_one("[data-total-page]")
        return _to_int(pagination.get("data-total-page")) if pagination else None

    def _parse_review_page(self, product_id: str, html: str) -> list[Review]:
        soup = BeautifulSoup(html, "lxml")
        reviews: list[Review] = []
        for row in soup.select("tr"):
            content_el = row.select_one("td.comment-content")
            if content_el is None:
                continue
            title_el = content_el.select_one(".comment-tit")
            text_el = content_el.select_one(".con")
            option_el = content_el.select_one(".pd-tit")
            info = [
                element.get_text(" ", strip=True)
                for element in row.select("td.info dl.writer-info dd")
            ]
            title = title_el.get_text(" ", strip=True) if title_el else ""
            content = text_el.get_text(" ", strip=True) if text_el else ""
            content = content or title
            if not content:
                continue
            option = option_el.get_text(" ", strip=True) if option_el else None
            author = info[0] if info else None
            written_at = _parse_date(info[1]) if len(info) > 1 else None

            reviews.append(
                Review(
                    platform=self.platform,
                    product_id=product_id,
                    review_id=_review_id(
                        product_id,
                        title,
                        content,
                        option,
                        author,
                        written_at.isoformat() if written_at else "",
                    ),
                    content=content,
                    author=author,
                    written_at=written_at,
                    option=option,
                )
            )
        return reviews

    @staticmethod
    def _append_unique(
        target: list[Review], seen: set[str], incoming: list[Review], limit: int
    ) -> int:
        added = 0
        for review in incoming:
            if review.review_id in seen:
                continue
            seen.add(review.review_id)
            target.append(review)
            added += 1
            if len(target) >= limit:
                break
        return added
