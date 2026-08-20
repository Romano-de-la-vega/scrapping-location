from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from watcher.models import Listing
from watcher.sites.seloger import (
    ADDRESS_SELECTOR,
    DESCRIPTION_SELECTOR,
    ENLARGED_CARD_SELECTOR,
    KEY_FACTS_SELECTOR,
    PRICE_SELECTOR,
    RESULT_CARD_SELECTOR,
    RESULT_CONTAINER_SELECTOR,
    RESULT_LINK_SELECTOR,
    SelogerAdapter,
)


SEARCH_URL = (
    "https://www.seloger.com/classified-search?distributionTypes=Rent"
    "&locations=POCOFR4450"
)
FIXTURE = Path(__file__).with_name("fixtures") / "seloger_results.html"
_VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta"}
)


@dataclass
class _Element:
    tag: str
    attrs: dict[str, str]
    parent: _Element | None = None
    children: list[_Element | str] = field(default_factory=list)

    def text_content(self) -> str:
        return "".join(
            child.text_content() if isinstance(child, _Element) else child
            for child in self.children
        )


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element("#document", {})
        self.stack = [self.root]

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        element = _Element(
            tag,
            {name: value or "" for name, value in attrs},
            self.stack[-1],
        )
        self.stack[-1].children.append(element)
        if tag not in _VOID_ELEMENTS:
            self.stack.append(element)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        element = _Element(
            tag,
            {name: value or "" for name, value in attrs},
            self.stack[-1],
        )
        self.stack[-1].children.append(element)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _descendants(element: _Element) -> list[_Element]:
    found: list[_Element] = []
    for child in element.children:
        if isinstance(child, _Element):
            found.append(child)
            found.extend(_descendants(child))
    return found


def _first_testid(element: _Element, testid: str) -> _Element | None:
    return next(
        (
            child
            for child in _descendants(element)
            if child.attrs.get("data-testid") == testid
        ),
        None,
    )


def _inside_testid(element: _Element, testid: str) -> bool:
    current: _Element | None = element
    while current is not None:
        if current.attrs.get("data-testid") == testid:
            return True
        current = current.parent
    return False


def _fixture_cards() -> list[_Element]:
    parser = _TreeBuilder()
    parser.feed(FIXTURE.read_text(encoding="utf-8"))
    container = next(
        element
        for element in _descendants(parser.root)
        if element.attrs.get("data-testid")
        == "serp-core-scrollablelistview-testid"
    )
    return [
        element
        for element in _descendants(container)
        if element.tag == "div"
        and element.attrs.get("data-testid")
        == "serp-core-classified-card-testid"
    ]


def _snapshot(card: _Element) -> dict[str, Any]:
    if _inside_testid(card, "serp-enlarged-card-testid"):
        return {"excluded": True}

    link = _first_testid(card, "card-mfe-covering-link-testid")

    def field_text(testid: str) -> str:
        node = _first_testid(card, testid)
        return node.text_content() if node is not None else ""

    return {
        "card_id": card.attrs.get("id", ""),
        "href": link.attrs.get("href", "") if link is not None else "",
        "title": link.attrs.get("title", "") if link is not None else "",
        "aria_label": (
            link.attrs.get("aria-label", "") if link is not None else ""
        ),
        "price": field_text("cardmfe-price-testid"),
        "keyfacts": field_text("cardmfe-keyfacts-testid"),
        "address": field_text("cardmfe-description-box-address"),
        "description": field_text("cardmfe-description-box-text-test-id"),
    }


class _FixtureElement:
    def __init__(self, element: _Element) -> None:
        self.element = element

    async def evaluate(self, script: str, selectors: dict[str, str]) -> dict[str, Any]:
        assert "element.closest(selectors.enlarged)" in script
        assert selectors == {
            "enlarged": ENLARGED_CARD_SELECTOR,
            "link": RESULT_LINK_SELECTOR,
            "price": PRICE_SELECTOR,
            "keyfacts": KEY_FACTS_SELECTOR,
            "address": ADDRESS_SELECTOR,
            "description": DESCRIPTION_SELECTOR,
        }
        return _snapshot(self.element)


class _FixtureLocator:
    def __init__(self, elements: list[_Element]) -> None:
        self.elements = elements

    async def count(self) -> int:
        return len(self.elements)

    def nth(self, index: int) -> _FixtureElement:
        return _FixtureElement(self.elements[index])


class _FixturePage:
    url = SEARCH_URL

    def __init__(self) -> None:
        self.requested_selector: str | None = None

    def locator(self, selector: str) -> _FixtureLocator:
        self.requested_selector = selector
        assert selector == RESULT_CARD_SELECTOR
        return _FixtureLocator(_fixture_cards())

    async def title(self) -> str:
        return "Locations Lyon 6e | SeLoger"

    async def goto(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("scan_results must not navigate")


class _DetailPage:
    def __init__(self, snapshot: object) -> None:
        self.snapshot = snapshot
        self.evaluate_calls = 0

    async def evaluate(self, script: str) -> object:
        self.evaluate_calls += 1
        assert 'text("h1")' in script
        assert "itemprop" in script
        assert "application/ld+json" not in script
        return self.snapshot

    async def goto(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("fetch_details must not navigate")


def test_live_result_selectors_are_scoped_and_specific() -> None:
    assert RESULT_CONTAINER_SELECTOR == (
        '[data-testid="serp-core-scrollablelistview-testid"]'
    )
    assert RESULT_CARD_SELECTOR.startswith(f"{RESULT_CONTAINER_SELECTOR} ")
    assert RESULT_LINK_SELECTOR == (
        'a[data-testid="card-mfe-covering-link-testid"]'
    )
    assert ENLARGED_CARD_SELECTOR not in RESULT_CARD_SELECTOR


def test_scan_results_parses_fixture_excludes_enlarged_and_deduplicates() -> None:
    adapter = SelogerAdapter(SEARCH_URL)
    page = _FixturePage()

    listings = asyncio.run(adapter.scan_results(page))

    assert [listing.listing_id for listing in listings] == [
        "WLCDP42",
        "ANNONCE9",
        "123456789",
    ]
    assert page.requested_selector == RESULT_CARD_SELECTOR

    first = listings[0]
    assert first.canonical_url == (
        "https://www.seloger.com/wl-cdp/WLCDP42?offer=keep"
    )
    assert first.title == "Appartement 2 pièces lumineux"
    assert first.price_eur == 637
    assert first.surface_m2 == 31.88
    assert first.rooms == 2
    assert first.location == "Lyon 6e"
    assert first.postal_code == "69006"

    assert listings[2].postal_code == "69003"
    assert listings[2].location == "Lyon 3e"
    assert all(listing.listing_id != "ENLARGED1" for listing in listings)
    assert all(listing.listing_id != "OUTSIDE1" for listing in listings)

    assert adapter.diagnostic() == {
        "site": "seloger",
        "loaded_url": SEARCH_URL,
        "page_title": "Locations Lyon 6e | SeLoger",
        "candidate_links": 5,
        "valid_ids": 4,
        "listings": 3,
        "duplicates": 1,
        "rejected_candidates": 1,
    }


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/wl-cdp/A1b2", "A1b2"),
        ("/annonce/location/appartement/lyon/AZ09", "AZ09"),
        (
            "/annonces/locations/appartement/lyon-6eme-69/987654321.htm",
            "987654321",
        ),
    ],
)
def test_extract_id_supports_the_three_live_url_shapes(
    url: str, expected: str
) -> None:
    assert SelogerAdapter(SEARCH_URL).extract_id(url) == expected


def test_card_source_id_must_be_consistent_except_known_legacy_urls() -> None:
    adapter = SelogerAdapter(SEARCH_URL)
    listing = adapter.parse_candidate(
        {
            "card_id": "classified-card-ROOT9",
            "href": "/wl-cdp/ROOT9",
            "price": "637 € /mois",
            "keyfacts": "2 pièces · 31,88 m²",
            "address": "69006 Lyon 6e",
        }
    )
    fallback = adapter.parse_candidate(
        {"card_id": "", "href": "/wl-cdp/URL8"}
    )

    assert listing is not None and listing.listing_id == "ROOT9"
    assert fallback is not None and fallback.listing_id == "URL8"
    assert adapter.parse_candidate(
        {
            "card_id": "classified-card-ROOT9",
            "href": "/wl-cdp/OTHER8",
        }
    ) is None
    legacy = adapter.parse_candidate(
        {
            "card_id": "classified-card-ROOT9",
            "href": "/annonces/locations/appartement/lyon/987654321.htm",
        }
    )
    assert legacy is not None and legacy.listing_id == "ROOT9"
    assert adapter.parse_candidate(
        {"card_id": "", "href": "/classified-search/result"}
    ) is None


def test_canonicalization_removes_only_manifest_tracking() -> None:
    adapter = SelogerAdapter(SEARCH_URL)
    assert adapter.canonicalize_url(
        "http://seloger.com/wl-cdp/ABC1/?utm_source=x&offer=keep&fbclid=y#photo"
    ) == "https://www.seloger.com/wl-cdp/ABC1?offer=keep"
    assert adapter.canonicalize_url(
        "https://www.seloger.com.evil.test/wl-cdp/ABC1"
    ) == ""


def test_fetch_details_parses_loaded_page_without_navigation() -> None:
    adapter = SelogerAdapter(SEARCH_URL)
    listing = Listing(
        site="seloger",
        listing_id="WLCDP42",
        canonical_url="https://www.seloger.com/wl-cdp/WLCDP42",
        title="Titre carte",
    )
    page = _DetailPage(
        {
            "h1": "Appartement T2 de 31,88 m²",
            "og_title": "Titre de secours",
            "og_url": (
                "https://seloger.com/wl-cdp/WLCDP42?utm_source=detail"
                "&offer=keep"
            ),
            "price": "637",
            "surface": "31,88",
            "rooms": "2",
            "locality": "Lyon 6e",
            "postal_code": "69006",
        }
    )

    detailed = asyncio.run(adapter.fetch_details(page, listing))

    assert page.evaluate_calls == 1
    assert detailed.listing_id == "WLCDP42"
    assert detailed.canonical_url == (
        "https://www.seloger.com/wl-cdp/WLCDP42?offer=keep"
    )
    assert detailed.title == "Appartement T2 de 31,88 m²"
    assert detailed.price_eur == 637
    assert detailed.surface_m2 == 31.88
    assert detailed.rooms == 2
    assert detailed.location == "Lyon 6e"
    assert detailed.postal_code == "69006"
