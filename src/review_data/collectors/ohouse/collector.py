"""오늘의집 상품 및 리뷰 collector.

현재 오늘의집은 headless Chromium 요청에 Access Denied를 반환합니다.
이 collector는 전달받은 설정의 복사본에만 ``headless=False``를 적용합니다.
전역 설정을 변경하거나 stealth 또는 브라우저 fingerprint 변경을 사용하지 않습니다.
"""

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from review_data.core.browser import BrowserCollector
from review_data.core.exceptions import ParseError
from review_data.core.models import Product, Review
from review_data.core.raw_payload import sanitize
from review_data.core.settings import Settings, get_settings

SEARCH_URL = "https://ohou.se/search/index"
PRODUCT_URL = "https://store.ohou.se/goods/{product_id}"
REVIEW_API_PATH = "/api/goods/reviews"
REVIEW_PAGE_SIZE = 5
VISIBLE_PRODUCT_LINKS = 'a[href*="/goods/"]:visible'

PRODUCT_ID_PATTERN = re.compile(r"/goods/(\d+)(?:[/?#]|$)")
PRICE_PATTERN = re.compile(r"^(\d[\d,]*)원$")
RATING_PATTERN = re.compile(r"^[0-5](?:\.\d+)?$")
REVIEW_COUNT_PATTERN = re.compile(r"^리뷰\s+([\d,]+)$")


class OhouseCollector(BrowserCollector):
    platform = "ohouse"
    label = "오늘의집"

    def __init__(self, settings: Settings | None = None) -> None:
        # 오늘의집은 실제 검증에서 headless Chromium에 403을 반환했습니다.
        # 원본/전역 Settings를 변경하지 않고 이 collector 전용 복사본만 사용합니다.
        source_settings = settings or get_settings()
        ohouse_settings = source_settings.model_copy(
            update={"headless": False},
            deep=True,
        )
        super().__init__(settings=ohouse_settings)

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        if limit <= 0:
            return []

        url = f"{SEARCH_URL}?{urlencode({'query': keyword})}"
        async with self.page() as page:
            await self._open_page(page, url)
            await self._wait_for_product_links(page, keyword)
            await self._load_search_results(page, limit)
            raw_products = await page.locator(VISIBLE_PRODUCT_LINKS).evaluate_all(
                """
                (anchors) => anchors.map((anchor) => {
                    const image = anchor.querySelector("img");
                    return {
                        href: anchor.href,
                        text: (anchor.innerText || "").trim(),
                        imageAlt: image?.alt || "",
                        imageSrc: image?.currentSrc || image?.src || "",
                        spanTexts: Array.from(anchor.querySelectorAll("span"))
                            .map((span) => (span.innerText || "").trim())
                            .filter(Boolean),
                    };
                })
                """
            )

        products: list[Product] = []
        seen_ids: set[str] = set()

        for item in raw_products:
            product = self._parse_search_product(item)
            if product is None or product.product_id in seen_ids:
                continue
            seen_ids.add(product.product_id)
            products.append(product)
            if len(products) >= limit:
                break

        if not products:
            raise ParseError(f"[{self.platform}] 검색 결과에서 상품을 찾지 못했습니다.")
        return products

    async def get_product(self, product_id: str) -> Product:
        url = PRODUCT_URL.format(product_id=product_id)
        async with self.page() as page:
            await self._open_page(page, url)
            data = await self._read_product_page_data(page)

        production = data.get("production")
        if not isinstance(production, dict):
            raise ParseError(f"[{self.platform}] 상품 상세 데이터가 없습니다.")

        actual_id = self._optional_str(production.get("id"))
        name = self._optional_str(production.get("name"))
        if not actual_id or not name:
            raise ParseError(f"[{self.platform}] 상품 필수 필드(id/name)를 찾지 못했습니다.")
        if actual_id != str(product_id):
            raise ParseError(
                f"[{self.platform}] 요청 상품 ID({product_id})와 응답 ID({actual_id})가 다릅니다."
            )

        categories = data.get("categories")
        category = None
        if isinstance(categories, list):
            titles = [
                str(item["title"]).strip()
                for item in categories
                if isinstance(item, dict) and item.get("title")
            ]
            category = " > ".join(titles) or None

        return Product(
            platform=self.platform,
            product_id=actual_id,
            name=name,
            url=url,
            brand=self._optional_str(production.get("brandName")),
            manufacturer=None,
            seller=None,
            price=self._optional_int(production.get("sellingPrice")),
            thumbnail_url=self._optional_str(production.get("originalImageUrl")),
            category=category,
            review_count=self._optional_int(production.get("reviewCount")),
            rating=self._optional_float(production.get("reviewAvg")),
        )

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        if limit <= 0:
            return []

        url = PRODUCT_URL.format(product_id=product_id)
        async with self.page() as page:
            await self._open_page(page, url)
            data = await self._read_product_page_data(page)

            review_data = data.get("review")
            if not isinstance(review_data, dict):
                raise ParseError(f"[{self.platform}] 리뷰 데이터를 찾지 못했습니다.")

            raw_reviews = review_data.get("reviews")
            if not isinstance(raw_reviews, list):
                raise ParseError(f"[{self.platform}] 리뷰 목록 형식이 올바르지 않습니다.")

            total_count = self._optional_int(review_data.get("totalCount")) or len(raw_reviews)
            page_number = 2

            while len(raw_reviews) < min(limit, total_count):
                await self.polite_wait()
                next_page = await self._fetch_review_page(page, product_id, page_number)
                page_reviews = next_page.get("reviews")
                if not isinstance(page_reviews, list):
                    raise ParseError(
                        f"[{self.platform}] 리뷰 {page_number}페이지 형식이 올바르지 않습니다."
                    )
                if not page_reviews:
                    break
                raw_reviews.extend(page_reviews)
                page_number += 1

        reviews: list[Review] = []
        seen_ids: set[str] = set()
        for item in raw_reviews:
            review = self._parse_review(item, str(product_id))
            if review is None or review.review_id in seen_ids:
                continue
            seen_ids.add(review.review_id)
            reviews.append(review)
            if len(reviews) >= limit:
                break
        return reviews

    async def _open_page(self, page: Page, url: str) -> None:
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError as exc:
            raise ParseError(f"[{self.platform}] 페이지 로딩 시간이 초과됐습니다: {url}") from exc

        status = response.status if response is not None else None
        title = await page.title()
        if status == 403 or "Access Denied" in title:
            raise ParseError(
                f"[{self.platform}] 표시형 Chromium에서도 접근이 거부됐습니다."
            )
        if status is not None and status >= 400:
            raise ParseError(f"[{self.platform}] 페이지 요청 실패: HTTP {status} ({url})")

    async def _wait_for_product_links(self, page: Page, keyword: str) -> None:
        try:
            await page.wait_for_selector(VISIBLE_PRODUCT_LINKS)
        except PlaywrightTimeoutError as exc:
            raise ParseError(f"[{self.platform}] 검색 결과 상품 링크를 찾지 못했습니다.") from exc
        try:
            await page.wait_for_function(
                "(keyword) => document.title.includes(keyword)",
                arg=keyword,
                timeout=10_000,
            )
        except PlaywrightTimeoutError as exc:
            raise ParseError(
                f"[{self.platform}] 검색어가 반영된 페이지를 확인하지 못했습니다: {keyword}"
            ) from exc
        await page.wait_for_timeout(500)

    async def _load_search_results(self, page: Page, limit: int) -> None:
        previous_count = -1
        unchanged_count = 0

        while await page.locator(VISIBLE_PRODUCT_LINKS).count() < limit:
            current_count = await page.locator(VISIBLE_PRODUCT_LINKS).count()
            if current_count == previous_count:
                unchanged_count += 1
            else:
                unchanged_count = 0
            if unchanged_count >= 2:
                break

            previous_count = current_count
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self.polite_wait()

    async def _read_product_page_data(self, page: Page) -> dict[str, Any]:
        locator = page.locator("script#__NEXT_DATA__")
        try:
            await locator.wait_for(state="attached")
            raw_data = await locator.text_content()
        except PlaywrightTimeoutError as exc:
            raise ParseError(f"[{self.platform}] __NEXT_DATA__를 찾지 못했습니다.") from exc

        if not raw_data:
            raise ParseError(f"[{self.platform}] __NEXT_DATA__가 비어 있습니다.")

        try:
            next_data = json.loads(raw_data)
            queries = next_data["props"]["pageProps"]["dehydratedState"]["queries"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ParseError(f"[{self.platform}] __NEXT_DATA__ 구조가 예상과 다릅니다.") from exc

        for query in queries:
            if not isinstance(query, dict):
                continue
            state = query.get("state")
            data = state.get("data") if isinstance(state, dict) else None
            if isinstance(data, dict) and isinstance(data.get("production"), dict):
                return data

        raise ParseError(f"[{self.platform}] __NEXT_DATA__에서 상품 데이터를 찾지 못했습니다.")

    async def _fetch_review_page(
        self, page: Page, product_id: str, page_number: int
    ) -> dict[str, Any]:
        result = await page.evaluate(
            """
            async ({path, productId, pageNumber, pageSize}) => {
                const query = new URLSearchParams({
                    page: String(pageNumber),
                    productionId: String(productId),
                    per: String(pageSize),
                    order: "best",
                    stars: "",
                    option: "",
                });
                const response = await fetch(`${path}?${query.toString()}`);
                return {
                    ok: response.ok,
                    status: response.status,
                    data: response.ok ? await response.json() : null,
                };
            }
            """,
            {
                "path": REVIEW_API_PATH,
                "productId": product_id,
                "pageNumber": page_number,
                "pageSize": REVIEW_PAGE_SIZE,
            },
        )

        if not isinstance(result, dict) or not result.get("ok"):
            status = result.get("status") if isinstance(result, dict) else None
            raise ParseError(
                f"[{self.platform}] 리뷰 {page_number}페이지 요청 실패: HTTP {status}"
            )
        data = result.get("data")
        if not isinstance(data, dict):
            raise ParseError(f"[{self.platform}] 리뷰 응답이 JSON 객체가 아닙니다.")
        return data

    def _parse_search_product(self, item: object) -> Product | None:
        if not isinstance(item, dict):
            return None

        href = self._optional_str(item.get("href"))
        span_texts = item.get("spanTexts")
        spans = (
            [str(value).strip() for value in span_texts if str(value).strip()]
            if isinstance(span_texts, list)
            else []
        )
        name = self._optional_str(item.get("imageAlt"))
        if not name and len(spans) >= 2:
            name = spans[1]
        if not href or not name:
            return None

        id_match = PRODUCT_ID_PATTERN.search(href)
        if id_match is None:
            return None
        product_id = id_match.group(1)

        lines = [
            line.strip()
            for line in str(item.get("text") or "").splitlines()
            if line.strip()
        ]
        try:
            name_index = lines.index(name)
        except ValueError:
            name_index = -1

        brand = spans[0] if spans else (lines[name_index - 1] if name_index > 0 else None)
        price = None
        rating = None
        review_count = None

        for index, line in enumerate(lines):
            if price is None and (price_match := PRICE_PATTERN.fullmatch(line)):
                price = int(price_match.group(1).replace(",", ""))
            if review_count is None and (
                review_match := REVIEW_COUNT_PATTERN.fullmatch(line)
            ):
                review_count = int(review_match.group(1).replace(",", ""))
                if index > 0 and RATING_PATTERN.fullmatch(lines[index - 1]):
                    rating = float(lines[index - 1])

        return Product(
            platform=self.platform,
            product_id=product_id,
            name=name,
            url=PRODUCT_URL.format(product_id=product_id),
            brand=brand,
            price=price,
            thumbnail_url=self._optional_str(item.get("imageSrc")),
            review_count=review_count,
            rating=rating,
        )

    def _parse_review(self, item: object, product_id: str) -> Review | None:
        if not isinstance(item, dict):
            return None

        review_id = self._optional_str(item.get("id"))
        review_detail = item.get("review")
        if not review_id or not isinstance(review_detail, dict):
            return None

        content = self._optional_str(review_detail.get("comment"))
        if not content:
            return None

        production_information = item.get("productionInformation")
        option = None
        if isinstance(production_information, dict):
            option = self._optional_str(production_information.get("explain"))

        card = item.get("card")
        images: list[str] = []
        if isinstance(card, dict):
            image_url = self._optional_str(card.get("imageUrl"))
            if image_url:
                images.append(image_url)

        written_at = None
        raw_date = self._optional_str(item.get("createdAt"))
        if raw_date:
            try:
                written_at = datetime.strptime(raw_date, "%Y.%m.%d")
            except ValueError as exc:
                raise ParseError(
                    f"[{self.platform}] 리뷰 작성일 형식이 예상과 다릅니다: {raw_date}"
                ) from exc

        return Review(
            platform=self.platform,
            product_id=product_id,
            review_id=review_id,
            content=content,
            rating=self._optional_float(review_detail.get("starAvg")),
            author=None,
            written_at=written_at,
            option=option,
            images=images,
            helpful_count=self._optional_int(item.get("praiseCount")),
            raw_payload=sanitize(item, content),
        )

    @staticmethod
    def _optional_str(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_float(value: object) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
