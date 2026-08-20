"""Deterministic Seventee extraction adapter.

The result selectors were observed after JavaScript rendering on the live
Seventee search page on 2026-08-20.  Its bounded hydration wait and generic
``h1``/Schema.org detail extraction were also exercised live that day, without
claiming that every field or future layout has been validated.
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


SEVENTEE_ORIGIN = "https://candidate.seventee.com"

# Live-observed result selectors (2026-08-20).  A div uses the same test ID as
# the picture, so the tag-qualified image selector must not be broadened.
RESULT_CONTAINER_SELECTOR = "#offers"
RESULT_LINK_SELECTOR = (
    '#offers a[data-testid="goToOffer"][href^="/offers/"]'
)
IMAGE_SELECTOR = 'img[data-testid="offerPicture"]'

MAX_CANDIDATE_TEXT = 1_500
MAX_TITLE_TEXT = 240

_SEVENTEE_HOST_RE = re.compile(r"candidate\.seventee\.com$", re.IGNORECASE)
_OFFER_ID_RE = re.compile(
    r"^/offers/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"/?$",
    re.IGNORECASE,
)


# Only the selected anchor and its qualified image are read.  No full card DOM
# serialization or whole-page text is involved.
_ANCHOR_SNAPSHOT_SCRIPT = """
(element, selectors) => {
    const image = element.querySelector(selectors.image);
    return {
        href: String(element.getAttribute("href") || "").slice(0, 2000),
        text: String(element.innerText || element.textContent || "")
            .slice(0, 1500),
        title: String(element.getAttribute("title") || "").slice(0, 240),
        aria_label: String(element.getAttribute("aria-label") || "")
            .slice(0, 240),
        image_alt: image
            ? String(image.getAttribute("alt") || "").slice(0, 240)
            : ""
    };
}
"""

_ANCHOR_SNAPSHOT_SELECTORS = {"image": IMAGE_SELECTOR}


# No JSON-LD was observed on the live result page.  Detail extraction remains
# a deliberately small, generic and explicitly unvalidated fallback.
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


def _candidate_title(candidate: Candidate) -> str | None:
    title = _first_text(
        candidate.get("title"),
        candidate.get("aria_label"),
        candidate.get("image_alt"),
    )
    return title[:MAX_TITLE_TEXT] if title is not None else None


class SeventeeAdapter(SiteAdapter):
    """Seventee result extraction and cautious generic detail enrichment."""

    site_name = "seventee"
    result_container_selector = RESULT_CONTAINER_SELECTOR
    result_link_selector = RESULT_LINK_SELECTOR
    image_selector = IMAGE_SELECTOR

    def canonicalize_url(self, url: str) -> str:
        canonical = canonicalize_tracking_url(url, base_url=SEVENTEE_ORIGIN)
        if not canonical:
            return ""
        parts = urlsplit(canonical)
        hostname = parts.hostname or ""
        if not _SEVENTEE_HOST_RE.fullmatch(hostname):
            return ""

        netloc = "candidate.seventee.com"
        if parts.port not in (None, 80, 443):
            netloc = f"{netloc}:{parts.port}"

        path = parts.path or "/"
        if match := _OFFER_ID_RE.fullmatch(path):
            path = f"/offers/{match.group(1).lower()}"
        return urlunsplit(("https", netloc, path, parts.query, ""))

    def extract_id(self, url: str) -> str | None:
        canonical = self.canonicalize_url(url)
        if not canonical:
            return None
        match = _OFFER_ID_RE.fullmatch(urlsplit(canonical).path)
        return match.group(1) if match else None

    def parse_candidate(self, candidate: Candidate) -> Listing | None:
        href = candidate.get("href")
        if href is None:
            return None
        canonical_url = self.canonicalize_url(href)
        listing_id = self.extract_id(canonical_url)
        if not canonical_url or listing_id is None:
            return None

        # The live cards expose their useful values in compact anchor text.
        # Parsing remains independent from eligibility: for example 102 m² is
        # a valid observation and must not be discarded here.
        text = str(candidate.get("text") or "")[:MAX_CANDIDATE_TEXT]
        postal_code = parse_postal_code(text)
        return Listing(
            site=self.site_name,
            listing_id=listing_id,
            canonical_url=canonical_url,
            title=_candidate_title(candidate),
            price_eur=parse_price(text),
            surface_m2=parse_surface(text),
            rooms=parse_rooms(text),
            location=parse_location(
                text, postal_code=postal_code, allow_plain=False
            ),
            postal_code=postal_code,
        )

    async def scan_results(self, page: Any) -> list[Listing]:
        links = page.locator(self.result_link_selector)
        # The live page is a JavaScript shell at DOMContentLoaded.  Wait for
        # the first observed offer only up to Playwright's configured selector
        # timeout; a timeout remains an unconfirmed empty result for the
        # runner's suspicious-result safeguard.
        await wait_for_first_result(links)
        candidate_count = int(await links.count())
        try:
            page_title = normalize_text(await page.title())
        except Exception:
            page_title = None
        loaded_url = normalize_text(getattr(page, "url", None))

        listings: list[Listing] = []
        identities: set[str] = set()
        valid_ids = 0
        duplicates = 0
        rejected = 0

        for index in range(candidate_count):
            snapshot = await links.nth(index).evaluate(
                _ANCHOR_SNAPSHOT_SCRIPT, _ANCHOR_SNAPSHOT_SELECTORS
            )
            if not isinstance(snapshot, Mapping):
                rejected += 1
                continue
            candidate = {
                key: (None if value is None else str(value))
                for key, value in snapshot.items()
                if key in {"href", "text", "title", "aria_label", "image_alt"}
            }
            listing = self.parse_candidate(candidate)
            if listing is None:
                rejected += 1
                continue
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
            candidate_links=candidate_count,
            valid_ids=valid_ids,
            listings=len(listings),
            duplicates=duplicates,
            rejected_candidates=rejected,
        )
        return listings

    async def fetch_details(self, page: Any, listing: Listing) -> Listing:
        if listing.site != self.site_name:
            raise ValueError("SeventeeAdapter cannot enrich another site's listing")
        if listing.canonical_url is None or listing.listing_id is None:
            raise ValueError("a Seventee source ID and canonical URL are required")
        detail_url = self.canonicalize_url(listing.canonical_url)
        detail_id = self.extract_id(detail_url)
        if detail_id is None or detail_id.casefold() != listing.listing_id.casefold():
            raise ValueError("the listing does not have a matching Seventee detail URL")

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
            location = parse_location(
                h1, postal_code=postal_code, allow_plain=False
            )

        canonical_url = detail_url
        if og_url := normalize_text(snapshot.get("og_url")):
            candidate_url = self.canonicalize_url(og_url)
            if self.extract_id(candidate_url) == detail_id:
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


Adapter = SeventeeAdapter
