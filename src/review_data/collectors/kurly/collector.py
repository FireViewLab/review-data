import json
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from review_data.core.base import BaseCollector
from review_data.core.exceptions import ParseError
from review_data.core.models import Product, Review
from review_data.core.product_description import parse_description


class KurlyCollector(BaseCollector):
    platform = "kurly"
    label = "마켓컬리"

    _BASE_URL = "https://www.kurly.com"

    _SEARCH_API_URL = (
        "https://api.kurly.com/search/v4/"
        "sites/market/normal-search"
    )

    _REVIEW_API_URL = (
        "https://api.kurly.com/product-review/v4/"
        "contents-products/{product_id}/reviews"
    )

    _REVIEW_PAGE_SIZE = 50

    async def _load_page_props(
        self,
        product_id: str,
    ) -> dict[str, Any]:
        """컬리 상품 페이지의 __NEXT_DATA__에서 pageProps를 추출합니다."""
        url = f"{self._BASE_URL}/goods/{product_id}"

        response = await self.client.get(url)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "lxml")
        script = soup.find("script", id="__NEXT_DATA__")

        if script is None:
            raise ParseError(
                f"kurly: 상품 {product_id} 페이지에서 "
                "__NEXT_DATA__를 찾지 못했습니다."
            )

        try:
            data = json.loads(script.get_text())
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"kurly: 상품 {product_id}의 JSON 해석에 실패했습니다."
            ) from exc

        page_props = data.get("props", {}).get("pageProps")

        if not isinstance(page_props, dict):
            raise ParseError(
                f"kurly: 상품 {product_id}의 pageProps 구조가 "
                "예상과 다릅니다."
            )

        return page_props

    @staticmethod
    def _extract_brand(
        product: dict[str, Any],
    ) -> str | None:
        """brandInfo에서 브랜드명을 추출합니다."""
        brand_info = product.get("brandInfo")

        if not isinstance(brand_info, dict):
            return None

        name_gate = brand_info.get("nameGate")

        if isinstance(name_gate, str):
            return name_gate.strip() or None

        if isinstance(name_gate, dict):
            for key in ("name", "brandName", "text"):
                value = name_gate.get(key)

                if isinstance(value, str) and value.strip():
                    return value.strip()

        return None

    @staticmethod
    def _extract_category(
        product: dict[str, Any],
    ) -> str | None:
        """categoryNames에서 불필요한 CATEGORY 값을 제거합니다."""
        category_names = product.get("categoryNames")

        if not isinstance(category_names, list):
            return None

        cleaned_categories = [
            str(item).strip()
            for item in category_names
            if item
            and str(item).strip()
            and str(item).strip().upper() != "CATEGORY"
        ]

        return " > ".join(cleaned_categories) or None

    @staticmethod
    def _extract_price(
        product: dict[str, Any],
    ) -> int | None:
        """할인가가 있으면 할인가를, 없으면 기본가를 반환합니다."""
        discounted_price = product.get("discountedPrice")
        base_price = product.get("basePrice")

        if isinstance(discounted_price, int):
            return discounted_price

        if isinstance(base_price, int):
            return base_price

        return None

    @staticmethod
    def _extract_search_price(
        raw_product: dict[str, Any],
    ) -> int | None:
        """검색 결과에서 할인가 또는 판매가를 추출합니다."""
        discounted_price = raw_product.get("discountedPrice")
        sales_price = raw_product.get("salesPrice")

        if isinstance(discounted_price, int):
            return discounted_price

        if isinstance(sales_price, int):
            return sales_price

        return None

    @staticmethod
    def _parse_search_review_count(
        value: Any,
    ) -> int | None:
        """
        검색 결과의 리뷰 수를 정수로 변환합니다.

        '9,999+'처럼 정확한 수가 아닌 값은 None으로 처리합니다.
        """
        if isinstance(value, int):
            return value

        if not isinstance(value, str):
            return None

        cleaned_value = value.strip()

        if not cleaned_value or "+" in cleaned_value:
            return None

        cleaned_value = cleaned_value.replace(",", "")

        if not cleaned_value.isdigit():
            return None

        return int(cleaned_value)

    def _parse_search_product(
        self,
        raw_product: dict[str, Any],
    ) -> Product | None:
        """컬리 검색 결과 상품을 공통 Product 모델로 변환합니다."""
        product_id = raw_product.get("no")
        name = raw_product.get("name")

        if product_id is None:
            return None

        if not isinstance(name, str) or not name.strip():
            return None

        thumbnail_url = raw_product.get("listImageUrl")

        if not isinstance(thumbnail_url, str):
            thumbnail_url = None

        return Product(
            platform=self.platform,
            product_id=str(product_id),
            name=name.strip(),
            url=f"{self._BASE_URL}/goods/{product_id}",
            brand=None,
            manufacturer=None,
            seller=None,
            price=self._extract_search_price(raw_product),
            thumbnail_url=thumbnail_url,
            category=None,
            review_count=self._parse_search_review_count(
                raw_product.get("reviewCount")
            ),
            rating=None,
        )

    @staticmethod
    def _parse_written_at(
        value: Any,
    ) -> datetime | None:
        """컬리 리뷰 작성일 문자열을 datetime으로 변환합니다."""
        if not isinstance(value, str) or not value.strip():
            return None

        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            return None

    @staticmethod
    def _extract_review_images(
        raw_review: dict[str, Any],
    ) -> list[str]:
        """컬리 리뷰 이미지 목록에서 이미지 URL을 추출합니다."""
        images: list[str] = []
        raw_images = raw_review.get("images")

        if isinstance(raw_images, list):
            for raw_image in raw_images:
                if not isinstance(raw_image, dict):
                    continue

                image_url = raw_image.get("image")

                if not isinstance(image_url, str) or not image_url.strip():
                    image_url = raw_image.get("reviewSquareSmallUrl")

                if isinstance(image_url, str) and image_url.strip():
                    cleaned_url = image_url.strip()

                    if cleaned_url not in images:
                        images.append(cleaned_url)

        fallback_image_url = raw_review.get("imageUrl")

        if (
            isinstance(fallback_image_url, str)
            and fallback_image_url.strip()
            and fallback_image_url.strip() not in images
        ):
            images.append(fallback_image_url.strip())

        return images

    def _parse_review(
        self,
        product_id: str,
        raw_review: dict[str, Any],
    ) -> Review | None:
        """컬리 원본 리뷰를 공통 Review 모델로 변환합니다."""
        review_id = raw_review.get("no")

        if review_id is None:
            review_id = raw_review.get("id")

        content = raw_review.get("contents")

        if review_id is None:
            return None

        if not isinstance(content, str) or not content.strip():
            return None

        author = raw_review.get("ownerName")

        if author is None:
            author = raw_review.get("author")

        if isinstance(author, str):
            author = author.strip() or None
        else:
            author = None

        helpful_count = raw_review.get("likeCount")

        if not isinstance(helpful_count, int):
            helpful_count = None

        return Review(
            platform=self.platform,
            product_id=str(product_id),
            review_id=str(review_id),
            content=content.strip(),
            rating=None,
            author=author,
            written_at=self._parse_written_at(
                raw_review.get("registeredAt")
            ),
            option=None,
            images=self._extract_review_images(raw_review),
            helpful_count=helpful_count,
        )

    async def search_products(
        self,
        keyword: str,
        limit: int = 20,
    ) -> list[Product]:
        """컬리 검색 API를 이용해 상품 목록을 수집합니다."""
        cleaned_keyword = keyword.strip()

        if not cleaned_keyword or limit <= 0:
            return []

        products: list[Product] = []
        seen_product_ids: set[str] = set()

        page = 1
        total_pages: int | None = None

        while len(products) < limit:
            response = await self.client.get(
                self._SEARCH_API_URL,
                params={
                    "keyword": cleaned_keyword,
                    "sortType": 4,
                    "page": page,
                },
                headers={
                    "Referer": f"{self._BASE_URL}/search",
                },
            )
            response.raise_for_status()

            try:
                payload = response.json()
            except ValueError as exc:
                raise ParseError(
                    f"kurly: 검색어 {cleaned_keyword!r}의 응답을 "
                    "JSON으로 해석하지 못했습니다."
                ) from exc

            if not isinstance(payload, dict):
                raise ParseError(
                    f"kurly: 검색어 {cleaned_keyword!r}의 응답 구조가 "
                    "예상과 다릅니다."
                )

            if payload.get("success") is False:
                message = payload.get("message")

                raise ParseError(
                    "kurly: 상품 검색 API가 실패를 반환했습니다. "
                    f"message={message!r}"
                )

            data = payload.get("data")

            if not isinstance(data, dict):
                raise ParseError(
                    f"kurly: 검색어 {cleaned_keyword!r}의 data 구조가 "
                    "예상과 다릅니다."
                )

            meta = data.get("meta")

            if isinstance(meta, dict):
                pagination = meta.get("pagination")

                if isinstance(pagination, dict):
                    raw_total_pages = pagination.get("totalPages")

                    if isinstance(raw_total_pages, int):
                        total_pages = raw_total_pages

            list_sections = data.get("listSections")

            if not isinstance(list_sections, list):
                raise ParseError(
                    f"kurly: 검색어 {cleaned_keyword!r}의 "
                    "listSections 구조가 예상과 다릅니다."
                )

            raw_products: list[dict[str, Any]] = []

            for section in list_sections:
                if not isinstance(section, dict):
                    continue

                section_data = section.get("data")

                if not isinstance(section_data, dict):
                    continue

                items = section_data.get("items")

                if not isinstance(items, list):
                    continue

                for item in items:
                    if isinstance(item, dict):
                        raw_products.append(item)

            if not raw_products:
                break

            for raw_product in raw_products:
                if len(products) >= limit:
                    break

                product = self._parse_search_product(raw_product)

                if product is None:
                    continue

                if product.product_id in seen_product_ids:
                    continue

                seen_product_ids.add(product.product_id)
                products.append(product)

            if total_pages is not None and page >= total_pages:
                break

            page += 1

            if len(products) < limit:
                await self.polite_wait()

        return products

    async def get_product(
        self,
        product_id: str,
    ) -> Product:
        """컬리 상품 상세 정보를 수집합니다."""
        page_props = await self._load_page_props(product_id)
        product = page_props.get("product")

        if not isinstance(product, dict):
            raise ParseError(
                f"kurly: 상품 {product_id} 정보를 찾지 못했습니다."
            )

        name = product.get("name")
        original_product_id = product.get("no")

        if not isinstance(name, str) or not name.strip():
            raise ParseError(
                f"kurly: 상품 {product_id}의 상품명이 없습니다."
            )

        if original_product_id is None:
            raise ParseError(
                f"kurly: 상품 {product_id}의 상품 번호가 없습니다."
            )

        review_count = product.get("reviewCount")

        if not isinstance(review_count, int):
            review_count = None

        seller = product.get("sellerName")

        if not isinstance(seller, str):
            seller = None

        thumbnail_url = product.get("mainImageUrl")

        if not isinstance(thumbnail_url, str):
            thumbnail_url = None

        detail = product.get("productDetail")
        detail = detail if isinstance(detail, dict) else {}
        content = detail.get("contentDescription")
        images = detail.get("legacyPiImages")
        images = images if isinstance(images, list) else []
        if isinstance(content, dict) and isinstance(content.get("productImages"), list):
            images = [*images, *content["productImages"]]
        while isinstance(content, dict):
            content = content.get("description")
        description_text, description_images = parse_description(
            content, f"{self._BASE_URL}/goods/{original_product_id}", images
        )

        return Product(
            platform=self.platform,
            product_id=str(original_product_id),
            name=name.strip(),
            url=f"{self._BASE_URL}/goods/{original_product_id}",
            brand=self._extract_brand(product),
            manufacturer=None,
            seller=seller,
            price=self._extract_price(product),
            thumbnail_url=thumbnail_url,
            description_text=description_text,
            description_images=description_images,
            category=self._extract_category(product),
            review_count=review_count,
            rating=None,
        )

    async def get_reviews(
        self,
        product_id: str,
        limit: int = 50,
    ) -> list[Review]:
        """컬리 리뷰 API를 커서 기반으로 순회해 리뷰를 수집합니다."""
        if limit <= 0:
            return []

        url = self._REVIEW_API_URL.format(
            product_id=product_id
        )
        referer = f"{self._BASE_URL}/goods/{product_id}"

        reviews: list[Review] = []
        seen_review_ids: set[str] = set()
        seen_cursors: set[str] = set()

        after: str | None = None

        while len(reviews) < limit:
            page_size = min(
                self._REVIEW_PAGE_SIZE,
                limit - len(reviews),
            )

            params: dict[str, str | int] = {
                "sortType": "RECENTLY",
                "size": page_size,
                "onlyImage": "false",
            }

            if after is not None:
                params["after"] = after

            response = await self.client.get(
                url,
                params=params,
                headers={"Referer": referer},
            )
            response.raise_for_status()

            try:
                payload = response.json()
            except ValueError as exc:
                raise ParseError(
                    f"kurly: 상품 {product_id}의 리뷰 응답을 "
                    "JSON으로 해석하지 못했습니다."
                ) from exc

            if not isinstance(payload, dict):
                raise ParseError(
                    f"kurly: 상품 {product_id}의 리뷰 응답 구조가 "
                    "예상과 다릅니다."
                )

            if payload.get("success") is False:
                message = payload.get("message")

                raise ParseError(
                    f"kurly: 상품 {product_id}의 리뷰 API가 "
                    f"실패를 반환했습니다. message={message!r}"
                )

            data = payload.get("data")

            if not isinstance(data, dict):
                raise ParseError(
                    f"kurly: 상품 {product_id}의 리뷰 data 구조가 "
                    "예상과 다릅니다."
                )

            raw_reviews = data.get("reviews")

            if not isinstance(raw_reviews, list):
                raise ParseError(
                    f"kurly: 상품 {product_id}의 reviews 구조가 "
                    "예상과 다릅니다."
                )

            if not raw_reviews:
                break

            for raw_review in raw_reviews:
                if len(reviews) >= limit:
                    break

                if not isinstance(raw_review, dict):
                    continue

                review = self._parse_review(
                    product_id=product_id,
                    raw_review=raw_review,
                )

                if review is None:
                    continue

                if review.review_id in seen_review_ids:
                    continue

                seen_review_ids.add(review.review_id)
                reviews.append(review)

            next_cursor = data.get("nextCursor")
            next_after: str | None = None

            if isinstance(next_cursor, dict):
                cursor_value = next_cursor.get("after")

                if cursor_value is not None:
                    next_after = str(cursor_value).strip() or None

            if next_after is None:
                break

            if next_after == after or next_after in seen_cursors:
                break

            seen_cursors.add(next_after)
            after = next_after

            if len(reviews) < limit:
                await self.polite_wait()

        return reviews
