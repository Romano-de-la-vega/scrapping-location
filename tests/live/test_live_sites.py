from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import pytest

from watcher.browser import BrowserSession
from watcher.config import load_config
from watcher.sites import create_adapter


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="set RUN_LIVE_TESTS=1 to enable network/browser checks",
    ),
]


async def _live_search(site: str) -> tuple[str | None, int]:
    root = Path(__file__).parents[2]
    configured = root / "config.json"
    config = load_config(
        configured if configured.exists() else root / "config.example.json"
    )
    adapter = create_adapter(site, config.sites[site].search_url)
    browser_config = replace(config.browser, headless=True)
    async with BrowserSession(browser_config, config.scan) as browser:
        async with browser.page() as page:
            await browser.navigate(page, adapter.search_url)
            challenge = await browser.challenge_reason(page)
            if challenge:
                return challenge, 0
            listings = await adapter.scan_results(page)
            return None, len(listings)


async def _live_seventee_detail() -> tuple[str | None, str | None]:
    root = Path(__file__).parents[2]
    configured = root / "config.json"
    config = load_config(
        configured if configured.exists() else root / "config.example.json"
    )
    adapter = create_adapter("seventee", config.sites["seventee"].search_url)
    browser_config = replace(config.browser, headless=True)
    async with BrowserSession(browser_config, config.scan) as browser:
        async with browser.page() as search_page:
            await browser.navigate(search_page, adapter.search_url)
            challenge = await browser.challenge_reason(search_page)
            if challenge:
                return challenge, None
            listings = await adapter.scan_results(search_page)
        assert listings
        async with browser.page() as detail_page:
            assert listings[0].canonical_url is not None
            await browser.navigate(detail_page, listings[0].canonical_url)
            challenge = await browser.challenge_reason(detail_page)
            if challenge:
                return challenge, None
            detail = await adapter.fetch_details(detail_page, listings[0])
            return None, detail.title


@pytest.mark.parametrize("site", ["leboncoin", "seloger", "seventee"])
def test_live_search_is_parseable_or_explicitly_challenged(site: str) -> None:
    challenge, count = asyncio.run(_live_search(site))
    assert challenge is not None or count > 0


def test_live_seventee_detail_hydrates_or_is_explicitly_challenged() -> None:
    challenge, title = asyncio.run(_live_seventee_detail())
    assert challenge is not None or title is not None
