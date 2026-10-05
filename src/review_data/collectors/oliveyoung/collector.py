"""
올리브영 collector.

데스크톱 웹은 봇 차단(403)이 있으나, 모바일 웹이 사용하는 공개 JSON API 는
인증 없이 접근 가능하다. 별도 우회 없이 모바일 UA + 공개 API 로만 수집한다.

- 검색  : 모바일 통합검색 API (POST, from/size 페이지네이션)
- 상세  : 모바일 상품 페이지 HTML 의 og 태그 + 내장 JSON 파싱
- 리뷰  : 모바일 리뷰 API (POST, cursorId/cursorScore 커서 페이지네이션)
"""

import re
from datetime import datetime

from bs4 import BeautifulSoup

from review_data.core.base import BaseCollector
from review_data.core.models import Product, Review

SEARCH_API = "https://m.oliveyoung.co.kr/search/api/v3/common/unified-search/goods"
DETAIL_PAGE = "https://m.oliveyoung.co.kr/m/goods/getGoodsDetail.do"
REVIEW_API = "https://m.oliveyoung.co.kr/review/api/v2/reviews/cursor"

# 상품 상세/리뷰의 표준 URL (수집 결과에는 접근성 좋은 데스크톱 주소를 남긴다)
PRODUCT_URL = "https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo={pid}"
REVIEW_IMAGE_BASE = "https://image.oliveyoung.co.kr/uploads/images/gdasEditor/"

REVIEW_PAGE_SIZE = 20

# 모바일 클라이언트로 위장해야 공개 API 가 정상 응답한다.
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
_API_HEADERS = {
    "User-Agent": _MOBILE_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR",
    "Origin": "https://m.oliveyoung.co.kr",
    "Referer": DETAIL_PAGE,
}
_PAGE_HEADERS = {"User-Agent": _MOBILE_UA, "Accept-Language": "ko-KR,ko;q=0.9"}

_BRAND_RE = re.compile(r'onlineBrandName\\?":\\?"([^"\\]+)')
_PRICE_RE = re.compile(r'finalPrice\\?":(\d+)')
_CATEGORY_RE = re.compile(r'middleCategoryName\\?":\\?"([^"\\]+)')
_DATE_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    match = _DATE_RE.search(text)
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    return datetime(year, month, day)


class OliveyoungCollector(BaseCollector):
    platform = "oliveyoung"
    label = "올리브영"

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        if not keyword or limit <= 0:
            return []

        body = {
            "query": keyword,
            "benefits": [],
            "sortCode": "POPULAR_ORDER",
            "displayMediaTypes": "Mobile",
            "from": 0,
            "size": max(limit, 20),
            "includeAll": True,
        }
        response = await self.client.post(SEARCH_API, json=body, headers=_API_HEADERS)
        response.raise_for_status()

        goods = response.json().get("data", {}).get("oliveGoods", {}).get("data", [])
        products: list[Product] = []
        for item in goods:
            product = self._parse_search_item(item)
            if product is not None:
                products.append(product)
            if len(products) >= limit:
                break
        return products

    def _parse_search_item(self, item: dict) -> Product | None:
        pid = item.get("goodsNumber")
        name = item.get("goodsName")
        if not pid or not name:
            return None

        score = item.get("goodsEvaluationScoreValue")
        return Product(
            platform=self.platform,
            product_id=pid,
            name=name,
            url=PRODUCT_URL.format(pid=pid),
            brand=item.get("onlineBrandName") or None,
            price=item.get("priceToPay") or item.get("minimumPriceToPay"),
            thumbnail_url=self._goods_thumbnail(item.get("imagePath")),
            category=item.get("middleCategoryName") or item.get("displayCategoryName") or None,
            review_count=item.get("goodsAssessmentTotalCount"),
            rating=float(score) if score else None,
        )

    def _goods_thumbnail(self, image_path: str | None) -> str | None:
        if not image_path:
            return None
        return (
            "https://image.oliveyoung.co.kr/cfimages/cf-goods/uploads/images/thumbnails/"
            + image_path
        )

    async def get_product(self, product_id: str) -> Product:
        response = await self.client.get(
            DETAIL_PAGE, params={"goodsNo": product_id}, headers=_PAGE_HEADERS
        )
        response.raise_for_status()
        html = response.text
        soup = BeautifulSoup(html, "lxml")

        name = self._meta(soup, "og:title").removesuffix(" | 올리브영").strip()
        thumbnail = self._meta(soup, "og:image") or None
        brand = self._first(_BRAND_RE, html)
        price = self._first(_PRICE_RE, html)
        category = self._first(_CATEGORY_RE, html)

        return Product(
            platform=self.platform,
            product_id=product_id,
            name=name,
            url=PRODUCT_URL.format(pid=product_id),
            brand=brand,
            price=int(price) if price else None,
            thumbnail_url=thumbnail,
            category=category,
        )

    def _meta(self, soup: BeautifulSoup, prop: str) -> str:
        tag = soup.find("meta", property=prop)
        return tag.get("content", "") if tag else ""

    def _first(self, pattern: re.Pattern[str], html: str) -> str | None:
        match = pattern.search(html)
        return match.group(1) if match else None

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        if limit <= 0:
            return []

        reviews: list[Review] = []
        seen: set[str] = set()
        cursor_id: int | None = None
        cursor_score: float | None = None

        while len(reviews) < limit:
            body: dict = {
                "goodsNumber": product_id,
                "size": REVIEW_PAGE_SIZE,
                "sortType": "USEFUL_SCORE_DESC",
            }
            if cursor_id is not None:
                body["cursorId"] = cursor_id
                body["cursorScore"] = cursor_score

            response = await self.client.post(REVIEW_API, json=body, headers=_API_HEADERS)
            response.raise_for_status()
            data = response.json().get("data", {})

            items = data.get("goodsReviewList") or []
            if not items:
                break

            for item in items:
                review = self._parse_review(product_id, item)
                if review is None or review.review_id in seen:
                    continue
                seen.add(review.review_id)
                reviews.append(review)
                if len(reviews) >= limit:
                    break

            if not data.get("hasNext"):
                break
            cursor_id = data.get("nextCursorId")
            cursor_score = data.get("nextCursorScore")
            if cursor_id is None:
                break
            await self.polite_wait()

        return reviews

    def _parse_review(self, product_id: str, item: dict) -> Review | None:
        review_id = item.get("reviewId")
        content = (item.get("content") or "").strip()
        # 내용 없이 별점만 남긴 리뷰는 표준 모델이 받지 않는다. 한 건 때문에 상품의 리뷰
        # 수집 전체가 실패하지 않도록 건너뛴다.
        if review_id is None or not content:
            return None

        profile = item.get("profileDto") or {}
        goods = item.get("goodsDto") or {}
        images = [
            REVIEW_IMAGE_BASE + photo["imagePath"]
            for photo in item.get("photoReviewList") or []
            if photo.get("imagePath")
        ]
        score = item.get("reviewScore")

        return Review(
            platform=self.platform,
            product_id=product_id,
            review_id=str(review_id),
            content=content,
            rating=float(score) if score is not None else None,
            author=profile.get("memberNickname") or None,
            written_at=_parse_date(item.get("createdDateTime")),
            option=goods.get("optionName") or None,
            images=images,
            helpful_count=item.get("recommendCount"),
        )
