from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from watcher.models import Listing
from watcher.sites.seventee import (
    IMAGE_SELECTOR,
    RESULT_CONTAINER_SELECTOR,
    RESULT_LINK_SELECTOR,
    SeventeeAdapter,
)


SEARCH_URL = (
    "https://candidate.seventee.com/location-appartement-maison.html"
    "?rentIncludingCharges.min=550%23RANGE%23%E2%82%AC"
)
FIRST_ID = "11111111-1111-4111-8111-111111111111"
SECOND_ID = "22222222-2222-4222-8222-222222222222"
THIRD_ID = "33333333-3333-4333-8333-333333333333"
FIXTURE = Path(__file__).with_name("fixtures") / "seventee_results.html"
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


def _fixture_links() -> list[_Element]:
    parser = _TreeBuilder()
    parser.feed(FIXTURE.read_text(encoding="utf-8"))
    offers = next(
        element
        for element in _descendants(parser.root)
        if element.attrs.get("id") == "offers"
    )
    return [
        element
        for element in _descendants(offers)
        if element.tag == "a"
        and element.attrs.get("data-testid") == "goToOffer"
        and element.attrs.get("href", "").startswith("/offers/")
    ]


def _snapshot(anchor: _Element) -> dict[str, str]:
    image = next(
        (
            element
            for element in _descendants(anchor)
            if element.tag == "img"
            and element.attrs.get("data-testid") == "offerPicture"
        ),
        None,
    )
    return {
        "href": anchor.attrs.get("href", ""),
        "text": anchor.text_content(),
        "title": anchor.attrs.get("title", ""),
        "aria_label": anchor.attrs.get("aria-label", ""),
        "image_alt": image.attrs.get("alt", "") if image is not None else "",
    }


class _FixtureElement:
    def __init__(self, element: _Element) -> None:
        self.element = element

    async def evaluate(
        self, script: str, selectors: dict[str, str]
    ) -> dict[str, str]:
        assert "document.body" not in script
        assert "element.querySelector(selectors.image)" in script
        assert selectors == {"image": IMAGE_SELECTOR}
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
        assert selector == RESULT_LINK_SELECTOR
        return _FixtureLocator(_fixture_links())

    async def title(self) -> str:
        return "Location appartement et maison Lyon 6e | Seventee"

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
        assert "document.body" not in script
        return self.snapshot

    async def goto(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("fetch_details must not navigate")


def test_live_result_selectors_are_exact_and_image_is_tag_qualified() -> None:
    assert RESULT_CONTAINER_SELECTOR == "#offers"
    assert RESULT_LINK_SELECTOR == (
        '#offers a[data-testid="goToOffer"][href^="/offers/"]'
    )
    assert IMAGE_SELECTOR == 'img[data-testid="offerPicture"]'
    assert not IMAGE_SELECTOR.startswith("div")


def test_scan_results_parses_fixture_deduplicates_and_reports_diagnostics() -> None:
    adapter = SeventeeAdapter(SEARCH_URL)
    page = _FixturePage()

    listings = asyncio.run(adapter.scan_results(page))

    assert [listing.listing_id for listing in listings] == [
        FIRST_ID,
        SECOND_ID,
        THIRD_ID,
    ]
    assert page.requested_selector == RESULT_LINK_SELECTOR

    first = listings[0]
    assert first.canonical_url == (
        f"https://candidate.seventee.com/offers/{FIRST_ID}?offer=keep"
    )
    assert first.title == "Appartement meublé Lyon 6e"
    assert first.price_eur == 740
    assert first.surface_m2 == 30.21
    assert first.rooms is None
    assert first.location == "Lyon"
    assert first.postal_code == "69006"

    second = listings[1]
    assert second.title == "Appartement rue Boileau"
    assert second.price_eur == 719
    assert second.surface_m2 == 36.5
    assert second.location == "Lyon"
    assert second.postal_code == "69006"

    # Extraction and business eligibility are deliberately separate.
    assert listings[2].surface_m2 == 102.0
    assert listings[2].rooms == 4

    assert adapter.diagnostic() == {
        "site": "seventee",
        "loaded_url": SEARCH_URL,
        "page_title": "Location appartement et maison Lyon 6e | Seventee",
        "candidate_links": 4,
        "valid_ids": 4,
        "listings": 3,
        "duplicates": 1,
        "rejected_candidates": 0,
    }


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"/offers/{FIRST_ID}", FIRST_ID),
        (f"/offers/{SECOND_ID}/?utm_source=x", SECOND_ID),
        ("/offers/not-a-uuid", None),
        (f"/offer/{FIRST_ID}", None),
        (f"/offers/{FIRST_ID}/extra", None),
    ],
)
def test_extract_id_uses_only_the_uuid_offer_segment(
    url: str, expected: str | None
) -> None:
    assert SeventeeAdapter(SEARCH_URL).extract_id(url) == expected


def test_canonicalization_removes_only_manifest_tracking() -> None:
    adapter = SeventeeAdapter(SEARCH_URL)
    uppercase_id = FIRST_ID.upper()
    assert adapter.canonicalize_url(
        "http://candidate.seventee.com/"
        f"offers/{uppercase_id}/?utm_source=x&offer=keep&fbclid=y#photo"
    ) == (
        f"https://candidate.seventee.com/offers/{FIRST_ID}?offer=keep"
    )
    assert adapter.canonicalize_url(
        f"https://candidate.seventee.com.evil.test/offers/{FIRST_ID}"
    ) == ""


@pytest.mark.parametrize(
    ("text", "price", "surface"),
    [
        ("Exclusivité Seventee 740,00 € 30.21 m² Meublé 69006 Lyon", 740, 30.21),
        ("719,00 € 36.5 m² 39 rue Boileau - 69006 LYON", 719, 36.5),
        ("780 € 102 m² 4 pièces 69006 Lyon", 780, 102.0),
    ],
)
def test_parse_candidate_uses_compact_anchor_text_without_filtering(
    text: str, price: int, surface: float
) -> None:
    listing = SeventeeAdapter(SEARCH_URL).parse_candidate(
        {"href": f"/offers/{THIRD_ID}", "text": text}
    )

    assert listing is not None
    assert listing.price_eur == price
    assert listing.surface_m2 == surface
    assert listing.postal_code == "69006"
    assert listing.location == "Lyon"


def test_fetch_details_uses_only_loaded_generic_semantics() -> None:
    adapter = SeventeeAdapter(SEARCH_URL)
    listing = Listing(
        site="seventee",
        listing_id=FIRST_ID,
        canonical_url=f"https://candidate.seventee.com/offers/{FIRST_ID}",
        title="Titre carte",
    )
    page = _DetailPage(
        {
            "h1": "Appartement T2 de 30,21 m²",
            "og_title": "Titre de secours",
            "og_url": (
                f"https://candidate.seventee.com/offers/{FIRST_ID}"
                "?utm_source=detail&offer=keep"
            ),
            "price": "740",
            "surface": "30,21",
            "rooms": "2",
            "locality": "Lyon 6e",
            "postal_code": "69006",
        }
    )

    detailed = asyncio.run(adapter.fetch_details(page, listing))

    assert page.evaluate_calls == 1
    assert detailed.listing_id == FIRST_ID
    assert detailed.canonical_url == (
        f"https://candidate.seventee.com/offers/{FIRST_ID}?offer=keep"
    )
    assert detailed.title == "Appartement T2 de 30,21 m²"
    assert detailed.price_eur == 740
    assert detailed.surface_m2 == 30.21
    assert detailed.rooms == 2
    assert detailed.location == "Lyon 6e"
    assert detailed.postal_code == "69006"
