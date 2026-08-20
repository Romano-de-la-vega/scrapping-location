"""Deterministic SeLoger extraction adapter.

The result selectors in this module were observed on the live SeLoger search
page on 2026-08-20.  Detail extraction deliberately uses only generic
metadata, ``h1`` and Schema.org ``itemprop`` values; those detail strategies
have not been validated against the live page.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from watcher.models import Listing
from watcher.sites.base import (
    AdapterDiagnostics,
    Candidate,
    SiteAdapter,
    canonicalize_tracking_url,
    normalize_text,
    parse_location,
    parse_postal_code,
    parse_price,
    parse_rooms,
    parse_surface,
    wait_for_detail_heading,
    wait_for_first_result,
)


SELOGER_ORIGIN = "https://www.seloger.com"

# Live-observed result selectors (2026-08-20).  Keep these centralized: result
# extraction must not broaden itself to generic anchors or whole-page text.
RESULT_CONTAINER_SELECTOR = (
    '[data-testid="serp-core-scrollablelistview-testid"]'
)
RESULT_CARD_SELECTOR = (
    f'{RESULT_CONTAINER_SELECTOR} '
    'div[data-testid="serp-core-classified-card-testid"]'
)
RESULT_LINK_SELECTOR = 'a[data-testid="card-mfe-covering-link-testid"]'
PRICE_SELECTOR = '[data-testid="cardmfe-price-testid"]'
KEY_FACTS_SELECTOR = '[data-testid="cardmfe-keyfacts-testid"]'
ADDRESS_SELECTOR = '[data-testid="cardmfe-description-box-address"]'
DESCRIPTION_SELECTOR = (
    '[data-testid="cardmfe-description-box-text-test-id"]'
)
ENLARGED_CARD_SELECTOR = '[data-testid="serp-enlarged-card-testid"]'

MAX_FIELD_TEXT = 1_500
MAX_TITLE_TEXT = 240

_SELOGER_HOST_RE = re.compile(
    r"(?:[a-z0-9-]+\.)*seloger\.com$", re.IGNORECASE
)
_CARD_ID_RE = re.compile(r"^classified-card-([A-Za-z0-9]+)$")
_WL_CDP_ID_RE = re.compile(r"^/wl-cdp/([A-Za-z0-9]+)/?$", re.IGNORECASE)
_ANNONCE_ID_RE = re.compile(
    r"^/annonce/(?:[^/]+/)*([A-Za-z0-9]+)/?$", re.IGNORECASE
)
_LEGACY_LOCATION_ID_RE = re.compile(
    r"^/annonces/locations/(?:[^/]+/)*([0-9]+)\.htm/?$",
    re.IGNORECASE,
)


# The script reads only the selected card and the five live-observed fields.
# ``closest`` is intentional: an enlarged unit is excluded even if it happens
# to contain a node shaped like a regular classified card.
_CARD_SNAPSHOT_SCRIPT = """
(element, selectors) => {
    if (element.closest(selectors.enlarged)) {
        return {excluded: true};
    }
    const link = element.querySelector(selectors.link);
    const boundedText = (selector, limit = 1500) => {
        const node = element.querySelector(selector);
        return node ? String(node.textContent || "").slice(0, limit) : "";
    };
    return {
        card_id: String(element.getAttribute("id") || "").slice(0, 240),
        href: link ? String(link.getAttribute("href") || "").slice(0, 2000) : "",
        title: link ? String(link.getAttribute("title") || "").slice(0, 240) : "",
        aria_label: link
            ? String(link.getAttribute("aria-label") || "").slice(0, 240)
            : "",
        price: boundedText(selectors.price),
        keyfacts: boundedText(selectors.keyfacts),
        address: boundedText(selectors.address),
        description: boundedText(selectors.description)
    };
}
"""

_CARD_SNAPSHOT_SELECTORS = {
    "enlarged": ENLARGED_CARD_SELECTOR,
    "link": RESULT_LINK_SELECTOR,
    "price": PRICE_SELECTOR,
    "keyfacts": KEY_FACTS_SELECTOR,
    "address": ADDRESS_SELECTOR,
    "description": DESCRIPTION_SELECTOR,
}


# These are generic semantic fallbacks only.  They are intentionally compact,
# avoid JSON-LD and body text, and do not navigate the page.
_DETAIL_SNAPSHOT_SCRIPT = """
() => {
    const attribute = (selector, name, limit = 500) => {
        const node = document.querySelector(selector);
        return node ? String(node.getAttribute(name) || "").slice(0, limit) : "";
    };
    const text = (selector, limit = 500) => {
        const node = document.querySelector(selector);
        return node ? String(node.textContent || "").slice(0, limit) : "";
    };
    return {
        h1: text("h1"),
        og_title: attribute('meta[property="og:title"]', "content"),
        og_url: attribute('meta[property="og:url"]', "content", 2000),
        price: attribute(
            'meta[itemprop="price"],meta[property="product:price:amount"]',
            "content"
        ) || text('[itemprop="price"]'),
        surface: attribute('[itemprop="floorSize"]', "content")
            || text('[itemprop="floorSize"]'),
        rooms: attribute('[itemprop="numberOfRooms"]', "content")
            || text('[itemprop="numberOfRooms"]'),
        locality: attribute('[itemprop="addressLocality"]', "content")
            || text('[itemprop="addressLocality"]'),
        postal_code: attribute('[itemprop="postalCode"]', "content")
            || text('[itemprop="postalCode"]')
    };
}
"""


def _first_text(*values: object | None) -> str | None:
    for value in values:
        if (text := normalize_text(value)) is not None:
            return text
    return None


def _card_source_id(value: object | None) -> str | None:
    text = normalize_text(value)
    if text is None:
        return None
    match = _CARD_ID_RE.fullmatch(text)
    return match.group(1) if match else None


def _path_source_id(path: str) -> str | None:
    for pattern in (_WL_CDP_ID_RE, _ANNONCE_ID_RE, _LEGACY_LOCATION_ID_RE):
        if match := pattern.fullmatch(path):
            return match.group(1)
    return None


def _candidate_title(candidate: Candidate) -> str | None:
    title = _first_text(
        candidate.get("title"),
        candidate.get("aria_label"),
        candidate.get("description"),
    )
    return title[:MAX_TITLE_TEXT] if title is not None else None


class SelogerAdapter(SiteAdapter):
    """SeLoger result and cautious detail extraction without filtering."""

    site_name = "seloger"
    result_container_selector = RESULT_CONTAINER_SELECTOR
    result_card_selector = RESULT_CARD_SELECTOR
    result_link_selector = RESULT_LINK_SELECTOR

    def canonicalize_url(self, url: str) -> str:
        canonical = canonicalize_tracking_url(url, base_url=SELOGER_ORIGIN)
        if not canonical:
            return ""
        parts = urlsplit(canonical)
        hostname = parts.hostname or ""
        if not _SELOGER_HOST_RE.fullmatch(hostname):
            return ""

        netloc = "www.seloger.com"
        if parts.port not in (None, 80, 443):
            netloc = f"{netloc}:{parts.port}"

        path = parts.path or "/"
        if _path_source_id(path) is not None:
            path = path.rstrip("/")
        return urlunsplit(("https", netloc, path, parts.query, ""))

    def extract_id(self, url: str) -> str | None:
        canonical = self.canonicalize_url(url)
        if not canonical:
            return None
        return _path_source_id(urlsplit(canonical).path)

    def parse_candidate(self, candidate: Candidate) -> Listing | None:
        href = candidate.get("href")
        canonical_url = self.canonicalize_url(href) if href is not None else ""

        # The root card ID is source-provided and therefore takes precedence.
        # URL extraction is only its fallback, never a synthetic replacement.
        card_id = _card_source_id(candidate.get("card_id"))
        url_id = self.extract_id(canonical_url) if canonical_url else None
        is_legacy_numeric_url = bool(
            canonical_url
            and _LEGACY_LOCATION_ID_RE.fullmatch(urlsplit(canonical_url).path)
        )
        if (
            card_id is not None
            and url_id is not None
            and card_id.casefold() != url_id.casefold()
            and not is_legacy_numeric_url
        ):
            return None
        listing_id = card_id or url_id
        if listing_id is None:
            return None

        price_text = str(candidate.get("price") or "")[:MAX_FIELD_TEXT]
        facts_text = str(candidate.get("keyfacts") or "")[:MAX_FIELD_TEXT]
        address_text = str(candidate.get("address") or "")[:MAX_FIELD_TEXT]
        postal_code = parse_postal_code(address_text)

        return Listing(
            site=self.site_name,
            listing_id=listing_id,
            canonical_url=canonical_url or None,
            title=_candidate_title(candidate),
            price_eur=parse_price(price_text),
            surface_m2=parse_surface(facts_text),
            rooms=parse_rooms(facts_text),
            location=parse_location(address_text, postal_code=postal_code),
            postal_code=postal_code,
        )

    async def scan_results(self, page: Any) -> list[Listing]:
        # BrowserSession owns navigation; this method only inspects the page
        # which is already loaded.
        cards = page.locator(self.result_card_selector)
        await wait_for_first_result(cards)
        card_count = int(await cards.count())
        try:
            page_title = normalize_text(await page.title())
        except Exception:
            page_title = None
        loaded_url = normalize_text(getattr(page, "url", None))

        listings: list[Listing] = []
        identities: set[str] = set()
        candidate_links = 0
        valid_ids = 0
        duplicates = 0
        rejected = 0

        for index in range(card_count):
            snapshot = await cards.nth(index).evaluate(
                _CARD_SNAPSHOT_SCRIPT, _CARD_SNAPSHOT_SELECTORS
            )
            if isinstance(snapshot, Mapping) and snapshot.get("excluded") is True:
                continue
            if not isinstance(snapshot, Mapping):
                rejected += 1
                continue

            candidate = {
                key: (None if value is None else str(value))
                for key, value in snapshot.items()
                if key
                in {
                    "card_id",
                    "href",
                    "title",
                    "aria_label",
                    "price",
                    "keyfacts",
                    "address",
                    "description",
                }
            }
            if normalize_text(candidate.get("href")) is not None:
                candidate_links += 1
            listing = self.parse_candidate(candidate)
            if listing is None:
                rejected += 1
                continue
            if listing.listing_id is not None:
                valid_ids += 1
            if listing.identity_key in identities:
                duplicates += 1
                continue
            identities.add(listing.identity_key)
            listings.append(listing)

        self._diagnostics = AdapterDiagnostics(
            site=self.site_name,
            loaded_url=loaded_url,
            page_title=page_title,
            candidate_links=candidate_links,
            valid_ids=valid_ids,
            listings=len(listings),
            duplicates=duplicates,
            rejected_candidates=rejected,
        )
        return listings

    async def fetch_details(self, page: Any, listing: Listing) -> Listing:
        if listing.site != self.site_name:
            raise ValueError("SelogerAdapter cannot enrich another site's listing")
        if listing.canonical_url is None:
            raise ValueError("a canonical detail URL is required")
        detail_url = self.canonicalize_url(listing.canonical_url)
        if not detail_url:
            raise ValueError("the listing does not have a valid SeLoger URL")

        # No page.goto here: BrowserSession has already loaded detail_url.
        await wait_for_detail_heading(page)
        snapshot = await page.evaluate(_DETAIL_SNAPSHOT_SCRIPT)
        if not isinstance(snapshot, Mapping):
            return listing

        h1 = normalize_text(snapshot.get("h1"))
        title = _first_text(h1, snapshot.get("og_title"))
        price = parse_price(snapshot.get("price"), allow_bare=True)
        surface = parse_surface(snapshot.get("surface"), allow_bare=True)
        if surface is None:
            surface = parse_surface(h1)
        rooms = parse_rooms(snapshot.get("rooms"), allow_bare=True)
        if rooms is None:
            rooms = parse_rooms(h1)
        postal_code = parse_postal_code(snapshot.get("postal_code"))
        location = parse_location(
            snapshot.get("locality"), postal_code=postal_code
        )
        if location is None:
            location = parse_location(h1, postal_code=postal_code)

        canonical_url = detail_url
        if og_url := normalize_text(snapshot.get("og_url")):
            candidate_url = self.canonicalize_url(og_url)
            current_url_id = self.extract_id(detail_url)
            candidate_url_id = self.extract_id(candidate_url)
            if candidate_url_id is not None and candidate_url_id in {
                listing.listing_id,
                current_url_id,
            }:
                canonical_url = candidate_url

        return Listing(
            site=self.site_name,
            listing_id=listing.listing_id,
            canonical_url=canonical_url,
            title=title or listing.title,
            price_eur=price if price is not None else listing.price_eur,
            surface_m2=(
                surface if surface is not None else listing.surface_m2
            ),
            rooms=rooms if rooms is not None else listing.rooms,
            location=location or listing.location,
            postal_code=postal_code or listing.postal_code,
            eligibility=listing.eligibility,
        )


# Both common spellings are exported for registries and direct imports.
SeLogerAdapter = SelogerAdapter
Adapter = SelogerAdapter
