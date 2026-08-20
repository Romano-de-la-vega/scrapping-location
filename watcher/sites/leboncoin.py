"""Deterministic Leboncoin extraction adapter.

The result-page selector is the one documented in the specification.  It has
not been validated against a live page in this project because the available
session was stopped by DataDome; the offline fixture mirrors only that known
URL pattern.  Challenge detection belongs to :mod:`watcher.browser`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from watcher.models import Listing
from watcher.sites.base import (
    AdapterDiagnostics,
    Candidate,
    SiteAdapter,
    canonicalize_tracking_url,
    compact_lines,
    normalize_text,
    parse_location,
    parse_postal_code,
    parse_price,
    parse_rooms,
    parse_surface,
    wait_for_detail_heading,
    wait_for_first_result,
)


LEBONCOIN_ORIGIN = "https://www.leboncoin.fr"
RESULT_LINK_SELECTOR = 'a[href*="/ad/locations/"]'
MAX_CANDIDATE_TEXT = 1_500
MAX_JSON_LD_DOCUMENTS = 20
MAX_JSON_LD_TEXT = 32_768

_DETAIL_ID_RE = re.compile(r"^/ad/locations/([0-9]+)/?$")
_LEBONCOIN_HOST_RE = re.compile(
    r"(?:[a-z0-9-]+\.)*leboncoin\.fr$", re.IGNORECASE
)


# The script is scoped to the already selected anchor.  In particular it never
# reads document.body or serializes a card/DOM subtree.
_ANCHOR_SNAPSHOT_SCRIPT = """
(element, limit) => ({
    href: element.getAttribute("href") || "",
    text: String(element.innerText || element.textContent || "").slice(0, limit),
    title: String(element.getAttribute("title") || "").slice(0, 240),
    aria_label: String(element.getAttribute("aria-label") || "").slice(0, 240)
})
"""


# These detail strategies use standardized metadata, semantic itemprop values
# and the page's h1.  They are cautious fallbacks, not claims about a currently
# live-validated Leboncoin DOM.  Every returned string is bounded in-page.
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
            || text('[itemprop="postalCode"]'),
        json_ld: Array.from(
            document.querySelectorAll('script[type="application/ld+json"]')
        ).slice(0, 20).map(
            node => String(node.textContent || "").slice(0, 32768)
        )
    };
}
"""


def _one(values: Iterable[Any]) -> Any | None:
    unique: list[Any] = []
    for value in values:
        if value is not None and value not in unique:
            unique.append(value)
    return unique[0] if len(unique) == 1 else None


def _first_text(*values: object | None) -> str | None:
    for value in values:
        if (text := normalize_text(value)) is not None:
            return text
    return None


def _candidate_title(candidate: Candidate) -> str | None:
    explicit = _first_text(candidate.get("title"), candidate.get("aria_label"))
    if explicit is not None:
        return explicit[:240]
    lines = compact_lines(candidate.get("text"), limit=MAX_CANDIDATE_TEXT)
    return lines[0][:240] if lines else None


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _schema_type(record: Mapping[str, Any]) -> set[str]:
    raw = record.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return {
        str(value).rsplit("/", 1)[-1].casefold()
        for value in values
        if isinstance(value, str)
    }


_LISTING_SCHEMA_TYPES = frozenset(
    {
        "accommodation",
        "apartment",
        "house",
        "offer",
        "product",
        "realestatelisting",
        "residence",
        "singlefamilyresidence",
    }
)


def _walk_schema(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if _schema_type(value) & _LISTING_SCHEMA_TYPES:
            yield value
        for nested in value.values():
            yield from _walk_schema(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_schema(nested)


def _schema_records(chunks: object) -> list[Mapping[str, Any]]:
    if not isinstance(chunks, list):
        return []
    records: list[Mapping[str, Any]] = []
    for chunk in chunks[:MAX_JSON_LD_DOCUMENTS]:
        if not isinstance(chunk, str):
            continue
        try:
            decoded = json.loads(chunk[:MAX_JSON_LD_TEXT])
        except (json.JSONDecodeError, TypeError):
            continue
        records.extend(_walk_schema(decoded))
    return records


def _scalar(value: Any) -> Any | None:
    if isinstance(value, Mapping):
        for key in ("value", "amount"):
            if key in value and not isinstance(value[key], (Mapping, list)):
                return value[key]
        return None
    return None if isinstance(value, list) else value


def _schema_values(records: Iterable[Mapping[str, Any]], key: str) -> list[Any]:
    values: list[Any] = []
    for record in records:
        if key in record and (value := _scalar(record[key])) is not None:
            values.append(value)
    return values


def _schema_address_values(
    records: Iterable[Mapping[str, Any]], key: str
) -> list[Any]:
    values: list[Any] = []
    for record in records:
        address = _as_mapping(record.get("address"))
        if address is not None and key in address:
            value = _scalar(address[key])
            if value is not None:
                values.append(value)
    return values


class LeboncoinAdapter(SiteAdapter):
    """Leboncoin result and detail extraction with no business filtering."""

    site_name = "leboncoin"
    result_link_selector = RESULT_LINK_SELECTOR

    def canonicalize_url(self, url: str) -> str:
        canonical = canonicalize_tracking_url(url, base_url=LEBONCOIN_ORIGIN)
        if not canonical:
            return ""
        parts = urlsplit(canonical)
        hostname = parts.hostname or ""
        if not _LEBONCOIN_HOST_RE.fullmatch(hostname):
            return ""
        netloc = "www.leboncoin.fr"
        if parts.port not in (None, 80, 443):
            netloc = f"{netloc}:{parts.port}"
        path = parts.path
        if _DETAIL_ID_RE.fullmatch(path):
            path = path.rstrip("/")
        return urlunsplit(("https", netloc, path or "/", parts.query, ""))

    def extract_id(self, url: str) -> str | None:
        canonical = self.canonicalize_url(url)
        if not canonical:
            return None
        match = _DETAIL_ID_RE.fullmatch(urlsplit(canonical).path)
        return match.group(1) if match else None

    def parse_candidate(self, candidate: Candidate) -> Listing | None:
        href = candidate.get("href")
        if href is None:
            return None
        canonical_url = self.canonicalize_url(href)
        listing_id = self.extract_id(canonical_url)
        if not canonical_url or listing_id is None:
            return None

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
        # This is intentionally the sole result-card/listing selector.
        links = page.locator(self.result_link_selector)
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
                _ANCHOR_SNAPSHOT_SCRIPT, MAX_CANDIDATE_TEXT
            )
            if not isinstance(snapshot, Mapping):
                rejected += 1
                continue
            candidate = {
                key: (None if value is None else str(value))
                for key, value in snapshot.items()
                if key in {"href", "text", "title", "aria_label"}
            }
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
            candidate_links=candidate_count,
            valid_ids=valid_ids,
            listings=len(listings),
            duplicates=duplicates,
            rejected_candidates=rejected,
        )
        return listings

    async def fetch_details(self, page: Any, listing: Listing) -> Listing:
        if listing.site != self.site_name:
            raise ValueError("LeboncoinAdapter cannot enrich another site's listing")
        if listing.canonical_url is None:
            raise ValueError("a canonical detail URL is required")
        detail_url = self.canonicalize_url(listing.canonical_url)
        if not detail_url or self.extract_id(detail_url) != listing.listing_id:
            raise ValueError("the listing does not have a valid Leboncoin detail URL")

        await wait_for_detail_heading(page)
        snapshot = await page.evaluate(_DETAIL_SNAPSHOT_SCRIPT)
        if not isinstance(snapshot, Mapping):
            return listing

        records = _schema_records(snapshot.get("json_ld"))
        h1 = normalize_text(snapshot.get("h1"))
        title = _first_text(h1, snapshot.get("og_title"))
        if title is None:
            title = _one(
                normalize_text(value)
                for value in _schema_values(records, "name")
            )

        price = parse_price(snapshot.get("price"), allow_bare=True)
        if price is None:
            price = _one(
                parse_price(value, allow_bare=True)
                for value in _schema_values(records, "price")
            )

        surface = parse_surface(snapshot.get("surface"), allow_bare=True)
        if surface is None:
            surface = _one(
                parse_surface(value, allow_bare=True)
                for value in _schema_values(records, "floorSize")
            )
        if surface is None:
            surface = parse_surface(h1)

        rooms = parse_rooms(snapshot.get("rooms"), allow_bare=True)
        if rooms is None:
            rooms = _one(
                parse_rooms(value, allow_bare=True)
                for value in _schema_values(records, "numberOfRooms")
            )
        if rooms is None:
            rooms = parse_rooms(h1)

        postal_code = parse_postal_code(snapshot.get("postal_code"))
        if postal_code is None:
            postal_code = _one(
                parse_postal_code(value)
                for value in _schema_address_values(records, "postalCode")
            )

        location = parse_location(
            snapshot.get("locality"), postal_code=postal_code
        )
        if location is None:
            location = _one(
                parse_location(value, postal_code=postal_code)
                for value in _schema_address_values(records, "addressLocality")
            )
        if location is None:
            location = parse_location(
                h1, postal_code=postal_code, allow_plain=False
            )

        # og:url is accepted only if it resolves to the same source ID.
        canonical_url = detail_url
        if og_url := normalize_text(snapshot.get("og_url")):
            candidate_url = self.canonicalize_url(og_url)
            if self.extract_id(candidate_url) == listing.listing_id:
                canonical_url = candidate_url

        return Listing(
            site=self.site_name,
            listing_id=listing.listing_id,
            canonical_url=canonical_url,
            title=title or listing.title,
            price_eur=price if price is not None else listing.price_eur,
            surface_m2=surface if surface is not None else listing.surface_m2,
            rooms=rooms if rooms is not None else listing.rooms,
            location=location or listing.location,
            postal_code=postal_code or listing.postal_code,
            eligibility=listing.eligibility,
        )


# A concise alias is convenient for adapter registries.
Adapter = LeboncoinAdapter
