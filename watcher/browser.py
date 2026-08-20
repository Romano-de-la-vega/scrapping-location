"""Minimal Playwright lifecycle and bounded navigation helpers."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Route
from playwright.async_api import async_playwright

from watcher.config import BrowserConfig, ScanConfig


HEAVY_RESOURCE_TYPES = frozenset({"image", "font", "media"})
CHALLENGE_TEXT_PATTERN = re.compile(
    r"\b(captcha|datadome|access denied|acc[èe]s refus[ée]|"
    r"just a moment|verify you are human|"
    r"v[ée]rifiez que vous [êe]tes humain|security challenge|challenge)\b",
    re.IGNORECASE,
)
CHALLENGE_SELECTORS: tuple[str, ...] = (
    'script[src*="captcha-delivery.com"]',
    'iframe[src*="captcha"]',
    'iframe[src*="challenge"]',
    'script[src*="/challenge-platform/"]',
    '[id^="cf-chl-"]',
    '[id*="captcha" i]',
    '[class*="captcha" i]',
)


class BrowserSessionError(RuntimeError):
    """Base error raised by the browser abstraction."""


class NavigationError(BrowserSessionError):
    """Raised after the configured bounded navigation attempts fail."""


def should_block_resource(resource_type: str) -> bool:
    return resource_type.lower() in HEAVY_RESOURCE_TYPES


def challenge_text_reason(*values: str | None) -> str | None:
    """Return the first challenge marker found in compact page metadata."""

    for value in values:
        if value and (match := CHALLENGE_TEXT_PATTERN.search(value)):
            return match.group(0).lower()
    return None


class BrowserSession:
    """Own Playwright in launch mode, or borrow an existing CDP browser.

    In CDP mode only pages created by this session are closed.  The external
    browser and its pre-existing contexts/pages are deliberately left alone.
    """

    def __init__(
        self,
        browser_config: BrowserConfig,
        scan_config: ScanConfig | None = None,
    ) -> None:
        self.config = browser_config
        self.scan_config = scan_config or ScanConfig()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._owns_browser = False
        self._owns_context = False
        self._pages: list[Page] = []

    async def __aenter__(self) -> BrowserSession:
        try:
            self._playwright = await async_playwright().start()
            if self.config.mode == "launch":
                self._browser = await self._playwright.chromium.launch(
                    headless=self.config.headless
                )
                self._owns_browser = True
                self._context = await self._browser.new_context(locale="fr-FR")
                self._owns_context = True
            else:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    self.config.cdp_url,
                    timeout=self.config.navigation_timeout_ms,
                )
                if self._browser.contexts:
                    self._context = self._browser.contexts[0]
                else:
                    self._context = await self._browser.new_context(locale="fr-FR")
                    self._owns_context = True
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        for page in reversed(self._pages):
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass
        self._pages.clear()

        if self._context is not None and self._owns_context:
            try:
                await self._context.close()
            except Exception:
                pass
        self._context = None

        if self._browser is not None and self._owns_browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        self._browser = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._playwright = None

    async def new_page(self) -> Page:
        if self._context is None:
            raise BrowserSessionError("browser session is not open")
        page = await self._context.new_page()
        page.set_default_navigation_timeout(self.config.navigation_timeout_ms)
        page.set_default_timeout(self.config.selector_timeout_ms)
        if self.config.block_heavy_resources:

            async def route_handler(route: Route) -> None:
                if should_block_resource(route.request.resource_type):
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", route_handler)
        self._pages.append(page)
        return page

    @asynccontextmanager
    async def page(self) -> AsyncIterator[Page]:
        page = await self.new_page()
        try:
            yield page
        finally:
            if page in self._pages:
                self._pages.remove(page)
            if not page.is_closed():
                await page.close()

    async def navigate(self, page: Page, url: str) -> None:
        attempts = self.scan_config.navigation_retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.config.navigation_timeout_ms,
                )
                return
            except Exception as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
        raise NavigationError(
            f"navigation failed after {attempts} attempt(s): {url}"
        ) from last_error

    async def challenge_reason(self, page: Page) -> str | None:
        """Inspect compact metadata and known challenge elements only."""

        try:
            title = await page.title()
        except Exception:
            title = ""
        reason = challenge_text_reason(page.url, title)
        if reason:
            return reason
        for selector in CHALLENGE_SELECTORS:
            try:
                if await page.locator(selector).count():
                    return selector
            except Exception:
                continue
        try:
            headings = await page.locator("h1,h2,[role=heading]").all_inner_texts()
        except Exception:
            headings = []
        return challenge_text_reason(*headings[:20])

    async def bounded_lazy_load(
        self,
        page: Page,
        count_items: Callable[[], Awaitable[int]],
    ) -> int:
        """Scroll only while the observed card count grows, with a hard cap."""

        previous = await count_items()
        for _ in range(self.scan_config.max_lazy_scrolls):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(500)
            current = await count_items()
            if current <= previous:
                return current
            previous = current
        return previous

    async def save_debug_artifacts(
        self,
        page: Page,
        directory: str | Path,
        site: str,
        *,
        timestamp: datetime | None = None,
    ) -> dict[str, str]:
        """Save HTML and a screenshot locally after a parser error only."""

        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        moment = timestamp or datetime.now().astimezone()
        safe_site = re.sub(r"[^a-z0-9_-]+", "_", site.lower())
        stem = f"{moment:%Y%m%d_%H%M%S}_{safe_site}"
        screenshot_path = target / f"{stem}.png"
        html_path = target / f"{stem}.html"
        await page.screenshot(path=str(screenshot_path), full_page=False)
        html_path.write_text(await page.content(), encoding="utf-8")
        return {"screenshot": str(screenshot_path), "html": str(html_path)}
