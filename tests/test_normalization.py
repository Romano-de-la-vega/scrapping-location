"""Offline normalization and Leboncoin adapter tests.

No Playwright browser is launched: the small fake page implements only the
anchor operations used by the adapter and reads the synthetic local fixture.
"""

from __future__ import annotations

import asyncio
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from watcher.models import Listing
from watcher.sites.base import (
    SiteAdapter,
    canonicalize_tracking_url,
    parse_location,
    parse_postal_code,
    parse_price,
    parse_rooms,
    parse_surface,
    wait_for_first_result,
)
from watcher.sites.leboncoin import LeboncoinAdapter, RESULT_LINK_SELECTOR


FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_URL = (
    "https://www.leboncoin.fr/recherche?category=10&locations=Lyon_69006"
    "&sort=time&order=desc"
)


class _WaitableFirst:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.states: list[str] = []

    async def wait_for(self, *, state: str) -> None:
        self.states.append(state)
        if self.fail:
            raise TimeoutError("synthetic selector timeout")


class _WaitableLocator:
    def __init__(self, *, fail: bool = False) -> None:
        self.first = _WaitableFirst(fail=fail)


def test_first_result_wait_is_bounded_by_playwright_and_timeout_is_safe() -> None:
    ready = _WaitableLocator()
    timed_out = _WaitableLocator(fail=True)

    asyncio.run(wait_for_first_result(ready))
    asyncio.run(wait_for_first_result(timed_out))

    assert ready.first.states == ["attached"]
    assert timed_out.first.states == ["attached"]


class _FixtureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._anchor: dict[str, Any] | None = None
        self.anchors: list[dict[str, str]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "title":
            self._in_title = True
        if tag == "a" and self._anchor is None:
            values = {name: value or "" for name, value in attrs}
            self._anchor = {**values, "parts": []}

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._anchor is not None:
            anchor = self._anchor
            self._anchor = None
            anchor["text"] = "".join(anchor.pop("parts"))
            self.anchors.append(anchor)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._anchor is not None:
            self._anchor["parts"].append(data)


class _FakeAnchor:
    def __init__(self, data: dict[str, str]) -> None:
        self.data = data

    async def evaluate(self, script: str, limit: int) -> dict[str, str]:
        assert "document.body" not in script
        return {
            "href": self.data.get("href", ""),
            "text": self.data.get("text", "")[:limit],
            "title": self.data.get("title", ""),
            "aria_label": self.data.get("aria-label", ""),
        }


class _FakeLinks:
    def __init__(self, anchors: list[dict[str, str]]) -> None:
        self.anchors = anchors

    async def count(self) -> int:
        return len(self.anchors)

    def nth(self, index: int) -> _FakeAnchor:
        return _FakeAnchor(self.anchors[index])


class _FakeResultsPage:
    def __init__(self, fixture: Path) -> None:
        parser = _FixtureParser()
        parser.feed(fixture.read_text(encoding="utf-8"))
        self._title = parser.title
        self._anchors = parser.anchors
        self.url = SEARCH_URL
        self.selectors: list[str] = []

    def locator(self, selector: str) -> _FakeLinks:
        self.selectors.append(selector)
        if selector != RESULT_LINK_SELECTOR:
            raise AssertionError(f"unexpected result selector: {selector}")
        return _FakeLinks(
            [
                anchor
                for anchor in self._anchors
                if '/ad/locations/' in anchor.get("href", "")
            ]
        )

    async def title(self) -> str:
        return self._title


class _FakeDetailPage:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot

    async def evaluate(self, script: str) -> dict[str, Any]:
        assert "document.body" not in script
        assert 'script[type="application/ld+json"]' in script
        return self.snapshot


def test_common_numeric_parsers_are_explicit_and_conservative() -> None:
    assert parse_price("Loyer : 1\u202f250 € / mois") == 1_250
    assert parse_price("750 euros charges comprises") == 750
    assert parse_price("750") is None
    assert parse_price("750", allow_bare=True) == 750
    assert parse_price("750 € plus 50 € de charges") is None

    assert parse_surface("Surface 35,5 m²") == 35.5
    assert parse_surface("35") is None
    assert parse_surface("35", allow_bare=True) == 35.0
    assert parse_surface("35 m², jardin 10 m²") is None

    assert parse_rooms("Appartement T2 - 2 pièces") == 2
    assert parse_rooms("T2 annoncé comme 3 pièces") is None
    assert parse_rooms("deux pièces") is None


def test_postal_code_and_location_are_not_inferred() -> None:
    assert parse_postal_code("Lyon (69006)") == "69006"
    assert parse_postal_code("69006 ou 69100") is None
    assert parse_postal_code("identifiant 3252484841") is None

    assert parse_location("Lyon 6ème (69006)") == "Lyon 6e"
    assert parse_location("Villeurbanne (69100)", postal_code="69100") == "Villeurbanne"
    assert parse_location("Appartement rénové, code 69006") is None
    assert parse_location("69006") is None
    assert parse_location(
        "Exclusivité Seventee\n740,00 €\n30.21 m²\n69006\nLyon",
        postal_code="69006",
        allow_plain=False,
    ) == "Lyon"
    assert parse_location(
        "Exclusivité Seventee\n740,00 €", allow_plain=False
    ) is None


def test_tracking_canonicalization_preserves_identity_parameters() -> None:
    canonical = canonicalize_tracking_url(
        "HTTPS://Example.COM:443/ad?signature=z&utm_source=x&id=7&ref=mail#card"
    )
    assert canonical == "https://example.com/ad?id=7&ref=mail&signature=z"
    assert canonicalize_tracking_url("javascript:alert(1)") == ""
    assert canonicalize_tracking_url("https://user:secret@example.com/ad") == ""


def test_leboncoin_id_and_detail_url_normalization() -> None:
    adapter = LeboncoinAdapter(SEARCH_URL)
    raw = (
        "/ad/locations/3252484841?utm_campaign=test"
        "&campaign=identity&fbclid=x#result"
    )
    assert adapter.extract_id(raw) == "3252484841"
    assert adapter.canonicalize_url(raw) == (
        "https://www.leboncoin.fr/ad/locations/3252484841?campaign=identity"
    )
    assert adapter.extract_id("/ad/locations/not-an-id") is None
    assert adapter.extract_id("/ad/locations/123/extra") is None
    assert adapter.canonicalize_url(
        "https://not-leboncoin.example/ad/locations/3252484841"
    ) == ""


def test_search_url_is_kept_as_configured() -> None:
    configured = "https://www.leboncoin.fr/recherche?z=2&a=1&utm_source=kept"
    adapter = LeboncoinAdapter(configured)
    assert adapter.search_url == configured
    assert isinstance(adapter, SiteAdapter)


def test_parse_candidate_keeps_unknown_values_as_none() -> None:
    adapter = LeboncoinAdapter(SEARCH_URL)
    listing = adapter.parse_candidate(
        {
            "href": "/ad/locations/3252484999",
            "text": "Studio meublé\n620 €\nLyon 6e (69006)",
            "title": None,
            "aria_label": None,
        }
    )
    assert listing is not None
    assert listing.title == "Studio meublé"
    assert listing.price_eur == 620
    assert listing.surface_m2 is None
    assert listing.rooms is None
    assert listing.location == "Lyon 6e"
    assert listing.postal_code == "69006"


def test_fixture_scan_uses_known_selector_and_deduplicates() -> None:
    adapter = LeboncoinAdapter(SEARCH_URL)
    page = _FakeResultsPage(FIXTURES / "leboncoin_results.html")

    listings = asyncio.run(adapter.scan_results(page))

    assert page.selectors == [RESULT_LINK_SELECTOR]
    assert [listing.listing_id for listing in listings] == [
        "3252484841",
        "3252484999",
    ]
    first, second = listings
    assert first.canonical_url == (
        "https://www.leboncoin.fr/ad/locations/3252484841?campaign=keep-me"
    )
    assert first.title == "Appartement 2 pièces lumineux"
    assert (first.price_eur, first.surface_m2, first.rooms) == (750, 35.0, 2)
    assert (first.location, first.postal_code) == ("Lyon 6e", "69006")
    assert second.surface_m2 is None
    assert second.rooms is None

    assert adapter.diagnostic() == {
        "site": "leboncoin",
        "loaded_url": SEARCH_URL,
        "page_title": "Fixture synthétique Leboncoin — résultats",
        "candidate_links": 4,
        "valid_ids": 3,
        "listings": 2,
        "duplicates": 1,
        "rejected_candidates": 1,
    }


def test_detail_fetch_uses_only_bounded_semantic_sources() -> None:
    adapter = LeboncoinAdapter(SEARCH_URL)
    listing = Listing(
        site="leboncoin",
        listing_id="3252484841",
        canonical_url="https://www.leboncoin.fr/ad/locations/3252484841",
        title="Titre recherche",
        price_eur=750,
        surface_m2=None,
        rooms=None,
        location=None,
        postal_code=None,
    )
    page = _FakeDetailPage(
        {
            "h1": "Appartement T2",
            "og_title": "Titre OpenGraph",
            "og_url": (
                "https://www.leboncoin.fr/ad/locations/3252484841?utm_source=detail"
            ),
            "price": "720",
            "surface": "",
            "rooms": "",
            "locality": "Lyon 6ème",
            "postal_code": "69006",
            "json_ld": [
                '{"@type":"Apartment","floorSize":{"value":36},'
                '"numberOfRooms":2,"address":{"addressLocality":"Lyon",'
                '"postalCode":"69006"}}'
            ],
        }
    )

    enriched = asyncio.run(adapter.fetch_details(page, listing))

    assert enriched.title == "Appartement T2"
    assert enriched.price_eur == 720
    assert enriched.surface_m2 == 36.0
    assert enriched.rooms == 2
    assert enriched.location == "Lyon 6e"
    assert enriched.postal_code == "69006"
    assert enriched.canonical_url == listing.canonical_url
