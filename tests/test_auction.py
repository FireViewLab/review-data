"""옥션 차단은 selector 대기와 리뷰 요청 전에 실패한다 (네트워크/DB 없음)."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from review_data.collectors.auction.collector import (
    _BLOCKED_MARKER,
    AuctionCollector,
    _is_blocked_page,
)
from review_data.core.exceptions import ParseError
from review_data.core.settings import Settings


@pytest.fixture
def collector_page(monkeypatch):
    page = SimpleNamespace(
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        content=AsyncMock(return_value='<h1 class="itemtit">텀블러</h1>'),
        wait_for_selector=AsyncMock(),
        evaluate=AsyncMock(return_value=""),
    )
    closed = AsyncMock()

    @asynccontextmanager
    async def open_page():
        try:
            yield page
        finally:
            await closed()

    collector = AuctionCollector(Settings(_env_file=None))
    monkeypatch.setattr(collector, "page", open_page)
    monkeypatch.setattr(collector, "polite_wait", AsyncMock())
    return collector, page, closed


@pytest.mark.parametrize("method", ["search_products", "get_product", "get_reviews"])
@pytest.mark.parametrize(
    "status, html, message",
    [
        (403, "", "HTTP 403"),
        (200, "<title>Just a moment...</title>", "Cloudflare/CAPTCHA"),
        (200, "<title>잠시만 기다리십시오…</title>", "Cloudflare/CAPTCHA"),
        (200, "<title>Attention Required! | Cloudflare</title>", "Cloudflare/CAPTCHA"),
        (200, '<form id="challenge-form"></form>', "Cloudflare/CAPTCHA"),
        (200, '<form action="/cdn-cgi/challenge-platform/check"></form>', "Cloudflare/CAPTCHA"),
        (200, "<h1>Verify you are human</h1>", "Cloudflare/CAPTCHA"),
        (200, "<h1>CAPTCHA</h1>", "Cloudflare/CAPTCHA"),
        (200, "<h2>보안 문자를 입력해주세요</h2>", "Cloudflare/CAPTCHA"),
    ],
)
async def test_blocked_navigation_fails_before_selector_or_review_fetch(
    collector_page, method, status, html, message
):
    collector, page, closed = collector_page
    page.goto.return_value = SimpleNamespace(status=status)
    page.content.return_value = html

    with pytest.raises(ParseError, match=message) as caught:
        await getattr(collector, method)("123")

    assert "다시 시도" not in str(caught.value)
    page.goto.assert_awaited_once()
    page.wait_for_selector.assert_not_awaited()
    page.evaluate.assert_not_awaited()
    collector.polite_wait.assert_not_awaited()
    closed.assert_awaited_once()
    if status == 403:
        page.content.assert_not_awaited()


@pytest.mark.parametrize("method", ["search_products", "get_product", "get_reviews"])
@pytest.mark.parametrize("has_response", [True, False])
async def test_normal_page_keeps_existing_flow(collector_page, method, has_response):
    collector, page, closed = collector_page
    if not has_response:
        page.goto.return_value = None
    # 정상 페이지의 상품명/설명/스크립트에 등장하는 단어만으로 차단하지 않는다.
    page.content.return_value = """
        <title>옥션 상품</title><h1 class="itemtit">CAPTCHA 프린트 텀블러</h1>
        <p>Cloudflare / captcha / just a moment</p>
        <script>const captcha = 'verify you are human';</script>
        <div class="section--itemcard">
          <div class="area--itemcard_title"><a href="https://itempage3.auction.co.kr/DetailView.aspx?itemno=123">상품</a></div>
          <span class="text--title">텀블러</span>
        </div>
    """

    result = await getattr(collector, method)("123")

    selector = "div.section--itemcard" if method == "search_products" else "h1.itemtit"
    page.wait_for_selector.assert_awaited_once_with(selector)
    closed.assert_awaited_once()
    if method == "get_reviews":
        assert result == []
        page.evaluate.assert_awaited_once()
    elif method == "get_product":
        assert result.product_id == "123"
        assert result.name == "CAPTCHA 프린트 텀블러"
        page.evaluate.assert_not_awaited()
    else:
        assert result[0].product_id == "123"
        assert result[0].name == "텀블러"
        page.evaluate.assert_not_awaited()


@pytest.mark.parametrize("method", ["search_products", "get_product", "get_reviews"])
@pytest.mark.parametrize(
    "name", ["Just a Moment 앨범", "보안 문자를 입력해주세요 안내서", "CAPTCHA"]
)
async def test_product_titles_and_security_notices_are_not_block_pages(
    collector_page, method, name
):
    collector, page, closed = collector_page
    # 검색과 상세의 정상 콘텐츠 신호를 각각 독립적으로 검증한다.
    if method == "search_products":
        content = f"""
            <div class="section--itemcard">
              <div class="area--itemcard_title"><a href="https://itempage3.auction.co.kr/DetailView.aspx?itemno=123">{name}</a></div>
              <span class="text--title">{name}</span>
            </div>
        """
    else:
        content = f'<h1 class="itemtit">{name}</h1>'
    page.content.return_value = f"""
        <title>{name} - 옥션</title><h1>{name}</h1><h2>{name}</h2>
        <h2>보안 문자를 입력해주세요</h2>{content}
    """

    result = await getattr(collector, method)("123")

    page.wait_for_selector.assert_awaited_once()
    closed.assert_awaited_once()
    if method == "get_reviews":
        assert result == []
        page.evaluate.assert_awaited_once()
    elif method == "get_product":
        assert result.name == name
    else:
        assert result[0].name == name


@pytest.mark.parametrize("tag", ["title", "h1", "h2"])
@pytest.mark.parametrize(
    "text", ["Just a Moment 앨범", "보안 문자를 입력해주세요 안내서", "Verify you are human 포스터"]
)
def test_block_phrases_inside_titles_are_not_enough_without_product_signals(tag, text):
    assert not _is_blocked_page(f"<{tag}>{text}</{tag}>")


@pytest.mark.parametrize("method", ["search_products", "get_product", "get_reviews"])
@pytest.mark.parametrize(
    "status, block",
    [
        (403, ""),
        (200, "<title>Just a moment...</title>"),
        (200, '<form id="challenge-form"></form>'),
    ],
)
async def test_explicit_blocks_override_remaining_product_content(
    collector_page, method, status, block
):
    collector, page, closed = collector_page
    page.goto.return_value = SimpleNamespace(status=status)
    page.content.return_value = (
        block
        + """
        <h1 class="itemtit">Just a Moment 앨범</h1>
        <div class="section--itemcard">
          <div class="area--itemcard_title"><a href="?itemno=123">앨범</a></div>
        </div>
    """
    )

    with pytest.raises(ParseError, match="HTTP 403|Cloudflare/CAPTCHA"):
        await getattr(collector, method)("123")

    page.wait_for_selector.assert_not_awaited()
    page.evaluate.assert_not_awaited()
    page.goto.assert_awaited_once()
    collector.polite_wait.assert_not_awaited()
    closed.assert_awaited_once()


@pytest.mark.parametrize("method", ["search_products", "get_product", "get_reviews"])
async def test_selector_timeout_does_not_claim_confirmed_block(collector_page, method):
    collector, page, closed = collector_page
    timeout = PlaywrightTimeoutError("missing selector")
    page.wait_for_selector.side_effect = timeout

    with pytest.raises(ParseError, match="페이지 콘텐츠를 찾지 못했습니다") as caught:
        await getattr(collector, method)("123")

    assert caught.value.__cause__ is timeout
    assert "Cloudflare" not in str(caught.value)
    page.evaluate.assert_not_awaited()
    closed.assert_awaited_once()


async def test_review_api_block_fails_without_retry(collector_page):
    collector, page, closed = collector_page
    page.evaluate.side_effect = PlaywrightError(_BLOCKED_MARKER)

    with pytest.raises(ParseError, match="리뷰 API 접근이 차단") as caught:
        await collector.get_reviews("123")

    assert "다시 시도" not in str(caught.value)
    page.evaluate.assert_awaited_once()
    collector.polite_wait.assert_not_awaited()
    closed.assert_awaited_once()
