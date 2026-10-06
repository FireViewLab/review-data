"""Collector resource cleanup and browser failures without network or credentials."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from review_data.collectors.naver.collector import NaverCollector
from review_data.core.base import BaseCollector
from review_data.core.browser import BrowserCollector
from review_data.core.exceptions import NotSupportedError, ParseError
from review_data.core.settings import Settings


class Collector(BaseCollector):
    platform = "test"

    async def search_products(self, keyword, limit=20):
        return []

    async def get_product(self, product_id):
        raise NotSupportedError("test")

    async def get_reviews(self, product_id, limit=50):
        return []


class Browser(BrowserCollector, Collector):
    pass


@pytest.fixture
def resources(monkeypatch):
    client = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr("review_data.core.base.httpx.AsyncClient", Mock(return_value=client))
    context = SimpleNamespace(new_page=AsyncMock(), close=AsyncMock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock()
    )
    start = AsyncMock(return_value=playwright)
    monkeypatch.setattr(
        "review_data.core.browser.async_playwright", Mock(return_value=SimpleNamespace(start=start))
    )
    return SimpleNamespace(
        client=client, context=context, browser=browser, pw=playwright, start=start
    )


def settings():
    return Settings(_env_file=None)


@pytest.mark.parametrize("error", [ValueError("setup"), asyncio.CancelledError()])
async def test_setup_failure_closes_client(resources, error):
    collector = Collector(settings())
    collector.setup = AsyncMock(side_effect=error)
    collector.teardown = AsyncMock(side_effect=RuntimeError("cleanup"))
    with pytest.raises(type(error)) as caught:
        async with collector:
            pytest.fail("setup failure must not enter the block")
    assert caught.value is error
    collector.teardown.assert_awaited_once()
    resources.client.aclose.assert_awaited_once()
    assert collector._client is None


async def test_teardown_failure_still_closes_client(resources):
    collector = Collector(settings())
    collector.teardown = AsyncMock(side_effect=ValueError("cleanup"))
    with pytest.raises(ValueError, match="cleanup"):
        async with collector:
            pass
    resources.client.aclose.assert_awaited_once()
    assert collector._client is None


async def test_nested_entry_does_not_replace_client(resources):
    collector = Collector(settings())
    async with collector:
        with pytest.raises(RuntimeError, match="이미 사용"):
            await collector.__aenter__()
        assert collector.client is resources.client
    resources.client.aclose.assert_awaited_once()


async def test_naver_unsupported_search_does_not_start_browser(resources):
    async with NaverCollector(settings()) as collector:
        with pytest.raises(NotSupportedError, match="상품 검색"):
            await collector.search_products("test")
    resources.start.assert_not_awaited()
    resources.client.aclose.assert_awaited_once()


async def test_browser_is_reused_and_contexts_are_closed(resources):
    async with Browser(settings()) as collector:
        resources.start.assert_not_awaited()
        for _ in range(2):
            async with collector.page() as page:
                assert page is resources.context.new_page.return_value
    resources.start.assert_awaited_once()
    resources.pw.chromium.launch.assert_awaited_once_with(headless=True)
    assert resources.context.close.await_count == 2
    resources.browser.close.assert_awaited_once()
    resources.pw.stop.assert_awaited_once()


async def test_concurrent_pages_launch_only_one_browser(resources):
    async with Browser(settings()) as collector:

        async def visit():
            async with collector.page():
                await asyncio.sleep(0)

        await asyncio.gather(visit(), visit())
    resources.start.assert_awaited_once()
    resources.pw.chromium.launch.assert_awaited_once()


@pytest.mark.parametrize("stage", ["start", "launch", "context", "page", "body"])
async def test_playwright_failures_are_collector_errors(resources, stage):
    error = PlaywrightTimeoutError("browser timeout")
    target = {
        "start": resources.start,
        "launch": resources.pw.chromium.launch,
        "context": resources.browser.new_context,
        "page": resources.context.new_page,
    }.get(stage)
    if target is not None:
        target.side_effect = error
    collector = Browser(settings())
    with pytest.raises(ParseError, match="browser timeout") as caught:
        async with collector:
            async with collector.page():
                if stage == "body":
                    raise error
    assert caught.value.__cause__ is error
    resources.client.aclose.assert_awaited_once()
    if stage != "start":
        resources.pw.stop.assert_awaited_once()
    if stage in {"page", "body"}:
        resources.context.close.assert_awaited_once()
    assert collector._browser is None
    assert collector._playwright is None


async def test_launch_failure_can_be_retried_without_leaking_driver(resources):
    resources.pw.chromium.launch.side_effect = [PlaywrightError("launch"), resources.browser]
    async with Browser(settings()) as collector:
        with pytest.raises(ParseError, match="launch"):
            async with collector.page():
                pass
        resources.pw.stop.assert_awaited_once()
        async with collector.page():
            pass
    assert resources.start.await_count == 2
    assert resources.pw.stop.await_count == 2


async def test_browser_close_failure_still_stops_playwright(resources):
    resources.browser.close.side_effect = PlaywrightError("close")
    collector = Browser(settings())
    with pytest.raises(ParseError, match="close"):
        async with collector:
            async with collector.page():
                pass
    resources.pw.stop.assert_awaited_once()
    resources.client.aclose.assert_awaited_once()
    assert collector._browser is None
    assert collector._playwright is None


@pytest.mark.parametrize("error", [ValueError("body"), asyncio.CancelledError()])
async def test_cleanup_failures_preserve_body_error(resources, error):
    resources.context.close.side_effect = PlaywrightError("context close")
    resources.browser.close.side_effect = PlaywrightError("browser close")
    with pytest.raises(type(error)) as caught:
        async with Browser(settings()) as collector:
            async with collector.page():
                raise error
    assert caught.value is error
    resources.context.close.assert_awaited_once()
    resources.pw.stop.assert_awaited_once()
    resources.client.aclose.assert_awaited_once()


async def test_page_requires_active_collector(resources):
    with pytest.raises(RuntimeError, match="async with"):
        async with Browser(settings()).page():
            pass
    resources.start.assert_not_awaited()


async def test_cancelled_launch_stops_driver_and_closes_client(resources):
    resources.pw.chromium.launch.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        async with Browser(settings()) as collector:
            async with collector.page():
                pass
    resources.pw.stop.assert_awaited_once()
    resources.client.aclose.assert_awaited_once()


async def test_context_close_error_is_translated(resources):
    resources.context.close.side_effect = PlaywrightError("context close")
    with pytest.raises(ParseError, match="context close"):
        async with Browser(settings()) as collector:
            async with collector.page():
                pass
    resources.browser.close.assert_awaited_once()
    resources.pw.stop.assert_awaited_once()


@pytest.mark.parametrize("platform, status", [("naver", 501), ("test", 400)])
async def test_search_api_maps_unsupported_and_browser_errors(
    resources, monkeypatch, platform, status
):
    import importlib

    from fastapi import HTTPException

    api = importlib.import_module("review_data.api.app")

    class SearchingBrowser(Browser):
        async def search_products(self, keyword, limit=20):
            async with self.page():
                return []

    collector_cls = NaverCollector if platform == "naver" else SearchingBrowser
    monkeypatch.setattr(api, "_get_collector_cls", lambda _: lambda: collector_cls(settings()))
    resources.pw.chromium.launch.side_effect = PlaywrightError("launch unavailable")
    with pytest.raises(HTTPException) as caught:
        await api.search(platform, "test")
    assert caught.value.status_code == status
    if platform == "naver":
        resources.start.assert_not_awaited()
    else:
        assert "launch unavailable" in caught.value.detail
    resources.client.aclose.assert_awaited_once()
