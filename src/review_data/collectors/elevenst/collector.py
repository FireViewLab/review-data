"""
11번가 collector.

인증 키 없이 웹에서 공개적으로 접근 가능한 경로만 사용합니다.

- 검색  : apis.11st.co.kr 통합검색 JSON API (groupName == "list" 인 정식 노출 상품만)
- 상세  : 상품 페이지 HTML 안의 JSON-LD(application/ld+json) 파싱
- 리뷰  : 상품 리뷰 목록 HTML 조각(review-list)을 pageNo 로 순회하며 수집
"""

import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from review_data.core.base import BaseCollector
from review_data.core.exceptions import ParseError
from review_data.core.models import Product, Review

SEARCH_API = "https://apis.11st.co.kr/search/api/tab/total"
PRODUCT_PAGE = "https://www.11st.co.kr/products/{pid}"
REVIEW_LIST = "https://www.11st.co.kr/products/{pid}/review-list"

REVIEW_PAGE_SIZE = 20

# 리뷰 조각은 XHR 로만 정상 응답하므로 브라우저 요청처럼 헤더를 맞춰준다.
_XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest"}

_RATING_RE = re.compile(r"평점 별\s*\d+\s*점\s*중\s*(\d+(?:\.\d+)?)")
_DATE_RE = re.compile(r"(20\d{2})\.(\d{2})\.(\d{2})")
_BG_URL_RE = re.compile(r"url\(['\"]?(https?://[^'\")]+)")


def _to_int(text: str | None) -> int | None:
    """'2,402' 같은 표기를 정수로. 숫자가 없으면 None."""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


class ElevenstCollector(BaseCollector):
    platform = "elevenst"
    label = "11번가"

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        if not keyword or limit <= 0:
            return []

        response = await self.client.get(SEARCH_API, params={"kwd": keyword})
        response.raise_for_status()
        payload = response.json()

        products: list[Product] = []
        seen: set[str] = set()
        for module in payload.get("data", []):
            # groupName == "list" 만 정식 검색 노출 상품. 나머지는 광고/추천 영역.
            if module.get("groupName") != "list":
                continue
            for item in module.get("items", []):
                product = self._parse_search_item(item)
                if product is None or product.product_id in seen:
                    continue
                seen.add(product.product_id)
                products.append(product)
                if len(products) >= limit:
                    return products

        return products

    def _parse_search_item(self, item: dict) -> Product | None:
        pid = item.get("id")
        name = item.get("title")
        if not pid or not name:
            return None

        # linkUrl 은 광고 추적 주소인 경우가 있어 상품 페이지 표준 URL 로 통일한다.
        rating = item.get("satisfactionScore")

        return Product(
            platform=self.platform,
            product_id=str(pid),
            name=name,
            url=PRODUCT_PAGE.format(pid=pid),
            brand=item.get("brandEngNm") or None,
            seller=item.get("sellerNickName") or None,
            price=item.get("finalPrc"),
            thumbnail_url=item.get("imageUrl") or None,
            review_count=_to_int(item.get("reviewCountText")),
            rating=float(rating) if rating else None,
        )

    async def get_product(self, product_id: str) -> Product:
        response = await self.client.get(PRODUCT_PAGE.format(pid=product_id))
        response.raise_for_status()

        data = self._extract_ld_json(response.text)
        # 11번가는 없는 상품에도 200 을 준다. JSON-LD 가 없으면 상품이 없거나 페이지
        # 구조가 바뀐 것이므로, 이름 없는 상품을 만들어 성공으로 넘기면 안 된다.
        name = data.get("name")
        if not name:
            raise ParseError(
                f"[{self.platform}] 상품 정보를 찾지 못했습니다 (product_id={product_id}). "
                "없는 상품이거나 페이지 구조가 바뀌었습니다."
            )

        offers = data.get("offers") or {}
        brand = data.get("brand") or {}

        return Product(
            platform=self.platform,
            product_id=product_id,
            name=name,
            url=PRODUCT_PAGE.format(pid=product_id),
            brand=brand.get("name") or None,
            price=offers.get("price"),
            thumbnail_url=data.get("image") or None,
            category=data.get("category") or None,
        )

    def _extract_ld_json(self, html: str) -> dict:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict) and data.get("@type") == "Product":
                return data
        return {}

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        if limit <= 0:
            return []

        reviews: list[Review] = []
        seen: set[str] = set()
        page = 1
        referer = PRODUCT_PAGE.format(pid=product_id)

        while len(reviews) < limit:
            response = await self.client.get(
                REVIEW_LIST.format(pid=product_id),
                params={"pageSize": REVIEW_PAGE_SIZE, "pageNo": page},
                headers={**_XHR_HEADERS, "Referer": referer},
            )
            response.raise_for_status()

            elements = BeautifulSoup(response.text, "lxml").select("li.review_list_element")
            if not elements:
                break

            new_on_page = 0
            for element in elements:
                review = self._parse_review(product_id, element)
                if review is None or review.review_id in seen:
                    continue
                seen.add(review.review_id)
                reviews.append(review)
                new_on_page += 1
                if len(reviews) >= limit:
                    break

            # 더 이상 새 리뷰가 없으면(마지막 페이지 반복) 중단.
            if new_on_page == 0:
                break

            page += 1
            await self.polite_wait()

        return reviews

    def _parse_review(self, product_id: str, element) -> Review | None:
        review_id = element.get("data-contmapno")
        if not review_id:
            return None

        content_el = element.select_one(".cont_review_hide")
        content = content_el.get_text(" ", strip=True) if content_el else ""
        # 내용 없이 별점만 남긴 리뷰는 표준 모델이 받지 않는다. 한 건 때문에 상품의 리뷰
        # 수집 전체가 실패하지 않도록 건너뛴다.
        if not content:
            return None

        author_el = element.select_one(".c_product_reviewer")
        author = author_el.get_text(strip=True) if author_el else None

        block_text = element.get_text(" ", strip=True)

        rating_match = _RATING_RE.search(block_text)
        rating = float(rating_match.group(1)) if rating_match else None

        date_el = element.select_one(".date")
        written_at = self._parse_date(date_el.get_text(strip=True) if date_el else "")

        option_el = element.select_one(".c_product_review_cont .option")
        option = None
        if option_el:
            option = option_el.get_text(" ", strip=True).replace("선택 옵션", "").strip() or None

        images = [
            match.group(1)
            for tag in element.select(".c_product_review_thumbnail2 [style*=background-image]")
            if (match := _BG_URL_RE.search(tag.get("style", "")))
        ]

        kkuk_el = element.select_one("[class*=kkuk]")
        helpful = _to_int(kkuk_el.get_text(strip=True)) if kkuk_el else None

        return Review(
            platform=self.platform,
            product_id=product_id,
            review_id=str(review_id),
            content=content,
            rating=rating,
            author=author,
            written_at=written_at,
            option=option,
            images=images,
            helpful_count=helpful,
        )

    def _parse_date(self, text: str) -> datetime | None:
        match = _DATE_RE.search(text)
        if not match:
            return None
        year, month, day = (int(g) for g in match.groups())
        return datetime(year, month, day)
