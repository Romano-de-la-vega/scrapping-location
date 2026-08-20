from __future__ import annotations

import asyncio

import pytest

from watcher.browser import (
    BrowserSession,
    NavigationError,
    challenge_text_reason,
    should_block_resource,
)
from watcher.config import BrowserConfig, ScanConfig


class FakePage:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def goto(self, *_args, **_kwargs) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("simulated timeout")


def test_challenge_detection_uses_compact_metadata() -> None:
    assert challenge_text_reason("ordinary page", "Appartements Lyon") is None
    assert challenge_text_reason("Access denied") == "access denied"
    assert challenge_text_reason("DataDome CAPTCHA") == "datadome"
    assert challenge_text_reason("Just a moment…") == "just a moment"
    assert challenge_text_reason("Accès refusé") == "accès refusé"
    assert challenge_text_reason("/security/challenge?id=1") == "challenge"


def test_resource_policy_keeps_scripts_and_documents() -> None:
    assert should_block_resource("image")
    assert should_block_resource("font")
    assert should_block_resource("media")
    assert not should_block_resource("script")
    assert not should_block_resource("document")


def test_navigation_has_at_most_one_retry() -> None:
    session = BrowserSession(
        BrowserConfig(), ScanConfig(navigation_retries=1)
    )
    page = FakePage(failures=1)
    asyncio.run(session.navigate(page, "https://example.test"))
    assert page.calls == 2


def test_navigation_fails_after_bounded_attempts() -> None:
    session = BrowserSession(
        BrowserConfig(), ScanConfig(navigation_retries=1)
    )
    page = FakePage(failures=2)
    with pytest.raises(NavigationError):
        asyncio.run(session.navigate(page, "https://example.test"))
    assert page.calls == 2
