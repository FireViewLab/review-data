"""
옥션(auction.co.kr) collector.

검색/상세 페이지 모두 Cloudflare 챌린지로 보호되어 있어(httpx 로는 403),
Playwright 로 접근합니다. 리뷰는 상세 페이지가 호출하는 ReviewService(ASMX)
엔드포인트를 페이지 컨텍스트의 fetch 로 그대로 사용합니다
(브라우저가 이미 풀어둔 Cloudflare 클리어런스 쿠키를 재사용하기 위함).

⚠️ 알려진 제약: 옥션은 Cloudflare 봇 감지가 매우 엄격해서, 자동화된
Playwright 브라우저는 대부분 챌린지 화면("잠시만 기다리십시오")에서 막힙니다.
이 collector 는 실제 사이트 구조에 맞게 올바르게 구현되어 있지만, 봇 탐지를
우회하는 기법(스텔스 패치, 프록시 로테이션 등)은 의도적으로 사용하지 않습니다.
차단되면 원인을 알 수 있는 명확한 ParseError 로 실패합니다.
"""

import re
from datetime import datetime
from urllib.parse import quote

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from review_data.core.browser import BrowserCollector
from review_data.core.exceptions import ParseError
from review_data.core.models import Product, Review

SEARCH_URL = "https://www.auction.co.kr/n/search"
DETAIL_URL = "http://itempage3.auction.co.kr/DetailView.aspx"
REVIEW_API_PATH = "/WebService/ReviewService.asmx/GetReviewList"

_BLOCKED_MARKER = "AUCTION_BLOCKED"

# 응답이 JSON 이 아니면(Cloudflare 챌린지 HTML 등) 그대로 res.json() 을 호출해
# 알아보기 힘든 SyntaxError 로 죽는 대신, 명시적으로 구분 가능한 에러를 던진다.
_FETCH_REVIEW_LIST_JS = f"""
async ({{ path, itemNo, pageIndex }}) => {{
    const res = await fetch(path, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json;charset=utf-8" }},
        body: JSON.stringify({{
            itemNo,
            filterParam: "",
            sort: "popular",
            pageIndex,
        }}),
    }});
    const contentType = res.headers.get("content-type") || "";
    if (!res.ok || !contentType.includes("json")) {{
        throw new Error("{_BLOCKED_MARKER}");
    }}
    const data = await res.json();
    return data.d;
}}
"""


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


def _brand_text(scope: BeautifulSoup) -> str | None:
    el = scope.select_one(".text__brand")
    if el is None:
        return None
    inner = el.select_one(".text")
    text = (inner or el).get_text(strip=True)
    return text or None


def _is_blocked_page(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    # 일반 상품 설명이나 스크립트의 Cloudflare/CAPTCHA 언급은 차단 근거가 아니다.
    if soup.select_one(
        "form#challenge-form, form#challengeform, #cf-challenge-running, "
        "form[action*='/cdn-cgi/challenge-platform/']"
    ):
        return True
    markers = (
        "just a moment",
        "잠시만 기다리십시오",
        "attention required! | cloudflare",
        "verify you are human",
        "로봇이 아님을 확인",
        "보안 문자를 입력",
    )
    return any(
        any(marker in text for marker in markers)
        or text in {"captcha", "captcha verification", "captcha challenge"}
        for heading in soup.select("title, h1, h2")
        for text in [heading.get_text(" ", strip=True).lower()]
    )


async def _wait_for_real_page(
    page: Page, selector: str, platform: str, response: Response | None = None
) -> None:
    """확인된 차단은 즉시 실패하고, 정상 페이지에서만 콘텐츠를 기다린다."""
    if response is not None and response.status == 403:
        raise ParseError(f"{platform}: 페이지 접근이 차단되었습니다 (HTTP 403).")
    if _is_blocked_page(await page.content()):
        raise ParseError(
            f"{platform}: 페이지 접근이 차단되었습니다 (Cloudflare/CAPTCHA 차단 화면)."
        )
    try:
        await page.wait_for_selector(selector)
    except PlaywrightTimeoutError as exc:
        raise ParseError(
            f"{platform}: 페이지 콘텐츠를 찾지 못했습니다 (selector: {selector})."
        ) from exc


class AuctionCollector(BrowserCollector):
    platform = "auction"
    label = "옥션"

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        async with self.page() as page:
            response = await page.goto(
                f"{SEARCH_URL}?keyword={quote(keyword)}", wait_until="domcontentloaded"
            )
            await _wait_for_real_page(page, "div.section--itemcard", self.platform, response)
            content = await page.content()

        soup = BeautifulSoup(content, "lxml")
        products: list[Product] = []
        for card in soup.select("div.section--itemcard"):
            if len(products) >= limit:
                break

            link = card.select_one(".area--itemcard_title a[href*='itemno=']")
            if link is None:
                continue
            match = re.search(r"itemno=([A-Za-z0-9]+)", link["href"])
            if not match:
                continue
            product_id = match.group(1)

            name_el = card.select_one(".text--title")
            thumb_el = card.select_one("img.image--itemcard")
            price_el = card.select_one(".text__price-seller")
            seller_el = card.select_one(".section--itemcard_info_shop .text")
            rating_el = card.select_one(".list--score .awards .for-a11y")
            review_count_el = card.select_one(".reviewcnt .text--reviewcnt")

            products.append(
                Product(
                    platform=self.platform,
                    product_id=product_id,
                    name=name_el.get_text(strip=True) if name_el else "",
                    url=link["href"],
                    brand=_brand_text(card),
                    seller=seller_el.get_text(strip=True) if seller_el else None,
                    price=_parse_int(price_el.get_text() if price_el else None),
                    thumbnail_url=_fix_protocol(thumb_el["src"]) if thumb_el else None,
                    review_count=_parse_int(
                        review_count_el.get_text() if review_count_el else None
                    ),
                    rating=_parse_float(rating_el.get_text() if rating_el else None),
                )
            )
        return products

    async def get_product(self, product_id: str) -> Product:
        url = f"{DETAIL_URL}?itemno={product_id}"
        async with self.page() as page:
            response = await page.goto(url, wait_until="domcontentloaded")
            await _wait_for_real_page(page, "h1.itemtit", self.platform, response)
            content = await page.content()

        soup = BeautifulSoup(content, "lxml")

        name_el = soup.select_one("h1.itemtit")
        price_el = soup.select_one(".price_real")
        seller_el = soup.select_one("a.link__seller")
        thumb_el = soup.select_one(".box__viewer-container img")
        review_count_el = soup.select_one(".anchor__review .text__review-count")
        rating_el = next(
            (el for el in soup.select(".text__value") if "평점" in el.get_text()), None
        )
        category = " > ".join(el.get_text(strip=True) for el in soup.select("a.now"))

        return Product(
            platform=self.platform,
            product_id=product_id,
            name=name_el.get_text(strip=True) if name_el else "",
            url=url,
            brand=_brand_text(soup),
            seller=seller_el.get_text(strip=True) if seller_el else None,
            price=_parse_int(price_el.get_text() if price_el else None),
            thumbnail_url=_fix_protocol(thumb_el["src"]) if thumb_el else None,
            category=category or None,
            review_count=_parse_int(review_count_el.get_text() if review_count_el else None),
            rating=_parse_float(rating_el.get_text() if rating_el else None),
        )

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        reviews: list[Review] = []
        # 페이지당 리뷰 수를 알 수 없으므로(사이트 구조상 확인 불가) 최소 1건이라고
        # 가정해도 limit 을 채울 수 있게 여유를 둔다. 실제로는 훨씬 적은 페이지에서
        # 끝나며(빈 페이지를 만나면 즉시 종료), 이 값은 무한 루프 방지용 안전장치다.
        max_pages = limit + 10

        async with self.page() as page:
            response = await page.goto(
                f"{DETAIL_URL}?itemno={product_id}", wait_until="domcontentloaded"
            )
            await _wait_for_real_page(page, "h1.itemtit", self.platform, response)

            for page_index in range(1, max_pages + 1):
                if len(reviews) >= limit:
                    break
                try:
                    raw_html = await page.evaluate(
                        _FETCH_REVIEW_LIST_JS,
                        {
                            "path": REVIEW_API_PATH,
                            "itemNo": product_id,
                            "pageIndex": page_index,
                        },
                    )
                except PlaywrightError as exc:
                    if _BLOCKED_MARKER in str(exc):
                        raise ParseError(
                            f"{self.platform}: 리뷰 API 접근이 차단된 것으로 보입니다 "
                            "(HTTP 오류 또는 JSON이 아닌 응답)."
                        ) from exc
                    raise
                page_reviews = _parse_review_page(raw_html, self.platform, product_id)
                if not page_reviews:
                    break
                reviews.extend(page_reviews)
                if page_index < max_pages:
                    await self.polite_wait()

        return reviews[:limit]


def _parse_review_page(raw_html: str, platform: str, product_id: str) -> list[Review]:
    soup = BeautifulSoup(raw_html, "lxml")
    reviews: list[Review] = []

    for li in soup.select("li.list-item[data-review-seq]"):
        review_id = li["data-review-seq"]
        content_el = li.select_one(".box__review-text .text")
        content = content_el.get_text(strip=True) if content_el else ""
        # 내용 없이 별점만 남긴 리뷰는 표준 모델이 받지 않는다. 건너뛴다.
        if not content:
            continue
        author_el = li.select_one(".text__writer")
        date_el = li.select_one(".text__date")
        option_el = li.select_one(".text__option-selected")
        star_el = li.select_one(".box__star .image__star-fill")
        helpful_el = li.select_one(".box__helpful .text__count")

        rating = None
        if star_el is not None:
            match = re.search(r"(\d+(?:\.\d+)?)%", star_el.get("style", ""))
            if match:
                rating = round(float(match.group(1)) / 20, 1)

        written_at = None
        if date_el is not None:
            try:
                written_at = datetime.strptime(date_el.get_text(strip=True), "%Y.%m.%d")
            except ValueError:
                written_at = None

        images = []
        for thumb in li.select(".box__list-thumbnail a"):
            match = re.search(r"url\(([^)]+)\)", thumb.get("style", ""))
            if match:
                images.append(match.group(1))

        reviews.append(
            Review(
                platform=platform,
                product_id=product_id,
                review_id=review_id,
                content=content,
                rating=rating,
                author=author_el.get_text(strip=True) if author_el else None,
                written_at=written_at,
                option=option_el.get_text(strip=True) if option_el else None,
                images=images,
                helpful_count=_parse_int(helpful_el.get_text() if helpful_el else None),
            )
        )

    return reviews
