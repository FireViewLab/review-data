"""
JS 렌더링이 필요한 플랫폼용 베이스.

BaseCollector 를 상속하므로 self.client (httpx) 도 그대로 쓸 수 있습니다.
-> 상품은 API, 리뷰는 렌더링 같은 혼합 방식도 가능합니다.

브라우저는 collector 당 1개만 띄우고 재사용합니다.
페이지가 필요할 때마다 self.page() 컨텍스트를 사용하세요.

    async with self.page() as page:
        await page.goto(url)
        html = await page.content()
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import Browser, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from review_data.core.base import DEFAULT_HEADERS, BaseCollector
from review_data.core.exceptions import ParseError


class BrowserCollector(BaseCollector):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._browser_lock = asyncio.Lock()

    async def _ensure_browser(self) -> Browser:
        # 미지원 검색이나 HTTP 만 사용하는 작업은 브라우저가 필요하지 않다.
        async with self._browser_lock:
            if self._browser is None:
                try:
                    self._playwright = await async_playwright().start()
                    self._browser = await self._playwright.chromium.launch(
                        headless=self.settings.headless
                    )
                except BaseException:
                    try:
                        await self.teardown()
                    except Exception:
                        pass  # 초기화 실패의 원래 오류를 보존한다.
                    raise
            return self._browser

    async def teardown(self) -> None:
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        try:
            try:
                if browser is not None:
                    await browser.close()
            finally:
                if playwright is not None:
                    await playwright.stop()
        except PlaywrightError as exc:
            raise ParseError(f"[{self.platform}] 브라우저 종료 실패: {exc}") from exc

    @asynccontextmanager
    async def page(self) -> AsyncIterator[Page]:
        """새 브라우저 컨텍스트에서 페이지를 열고, 블록을 벗어나면 자동으로 닫습니다."""
        if self._client is None:
            raise RuntimeError(
                f"[{self.platform}] 'async with' 블록 안에서만 page 를 사용할 수 있습니다."
            )
        try:
            browser = await self._ensure_browser()
            context = await browser.new_context(
                user_agent=DEFAULT_HEADERS["User-Agent"],
                locale="ko-KR",
            )
            failed = False
            try:
                page = await context.new_page()
                yield page
            except BaseException:
                failed = True
                raise
            finally:
                try:
                    await context.close()
                except Exception:
                    if not failed:
                        raise
        except PlaywrightError as exc:
            raise ParseError(f"[{self.platform}] 브라우저 수집 실패: {exc}") from exc
