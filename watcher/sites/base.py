"""Small, deterministic building blocks shared by site adapters.

Adapters extract observations; they do not decide whether an observation is
eligible.  The helpers in this module deliberately return ``None`` when the
source text is missing or ambiguous instead of guessing a value.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from watcher.models import Listing


Candidate = Mapping[str, str | None]


_SPACE_RE = re.compile(r"\s+")
_PRICE_RE = re.compile(
    r"(?<![\d.,])"
    r"(\d{1,3}(?:(?:[ .\u00a0\u202f]\d{3})+)(?:,\d{1,2})?"
    r"|\d+(?:[.,]\d{1,2})?)"
    r"\s*(?:€|euros?|eur)(?![a-z])",
    re.IGNORECASE,
)
_SURFACE_RE = re.compile(
    r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*"
    r"(?:m\s*[²2]|m[eè]tres?\s+carr[ée]s?)(?![a-z])",
    re.IGNORECASE,
)
_ROOM_PATTERNS = (
    re.compile(r"(?<!\d)(\d{1,2})\s*(?:pi[eè]ces?|p\.)(?![a-z])", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])(?:t|f)\s*([1-9]\d?)(?!\d)", re.IGNORECASE),
)
_POSTAL_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")
_LYON_DISTRICT_RE = re.compile(
    r"\blyon\s*[- ]?\s*(\d{1,2})(?:er|e|eme|ème)?"
    r"(?:\s+arrondissement)?\b",
    re.IGNORECASE,
)
_LOCATION_SPLIT_RE = re.compile(r"[\n\r|•;]+")
_PLAIN_LOCATION_RE = re.compile(
    r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’ -]{0,59}"
)


# Only parameters whose common purpose is attribution/advertising are removed.
# Potentially semantic names such as ``id``, ``page``, ``ref`` and ``source``
# are intentionally preserved.
TRACKING_PARAMETER_NAMES = frozenset(
    {
        "_ga",
        "_gl",
        "dclid",
        "fbclid",
        "gad_source",
        "gclid",
        "gbraid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "srsltid",
        "wbraid",
    }
)


def normalize_text(value: object | None) -> str | None:
    """Collapse Unicode whitespace and return ``None`` for empty text."""

    if value is None:
        return None
    normalized = _SPACE_RE.sub(" ", str(value).replace("\u00ad", "")).strip()
    return normalized or None


def compact_lines(value: object | None, *, limit: int = 1_500) -> tuple[str, ...]:
    """Return non-empty, normalized lines from a bounded text fragment."""

    if value is None or limit <= 0:
        return ()
    bounded = str(value)[:limit]
    return tuple(
        line
        for raw_line in bounded.splitlines()
        if (line := normalize_text(raw_line)) is not None
    )


def _decimal(token: str) -> Decimal | None:
    compact = re.sub(r"[ \u00a0\u202f]", "", token)
    if not compact:
        return None

    # A dot followed by three digits is a French thousands separator.  Other
    # dots and commas are treated as decimal separators in the unit-bound
    # formats accepted by the public parsers.
    if "," in compact and "." in compact:
        decimal_separator = "," if compact.rfind(",") > compact.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        compact = compact.replace(thousands_separator, "")
        compact = compact.replace(decimal_separator, ".")
    elif "," in compact:
        compact = compact.replace(",", ".")
    elif compact.count(".") == 1:
        before, after = compact.split(".")
        if len(after) == 3 and len(before) <= 3:
            compact = before + after
    elif compact.count(".") > 1:
        compact = compact.replace(".", "")

    try:
        value = Decimal(compact)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _unique(values: list[Any]) -> Any | None:
    unique: list[Any] = []
    for value in values:
        if value is not None and value not in unique:
            unique.append(value)
    return unique[0] if len(unique) == 1 else None


def parse_price(text: object | None, *, allow_bare: bool = False) -> int | None:
    """Parse a unique explicit euro price.

    ``allow_bare`` is reserved for values read from a semantic ``price`` field
    (for example Schema.org metadata).  Free-form card text must include a euro
    marker, which prevents a surface or postal code from becoming a price.
    """

    value = normalize_text(text)
    if value is None:
        return None
    tokens = [match.group(1) for match in _PRICE_RE.finditer(value)]
    if allow_bare and not tokens and re.fullmatch(r"\d+(?:[.,]\d{1,2})?", value):
        tokens = [value]
    prices: list[int] = []
    for token in tokens:
        amount = _decimal(token)
        if amount is None or amount < 0 or amount != amount.to_integral_value():
            continue
        prices.append(int(amount))
    return _unique(prices)


def parse_surface(text: object | None, *, allow_bare: bool = False) -> float | None:
    """Parse a unique surface explicitly expressed in square metres."""

    value = normalize_text(text)
    if value is None:
        return None
    tokens = [match.group(1) for match in _SURFACE_RE.finditer(value)]
    if allow_bare and not tokens and re.fullmatch(r"\d+(?:[.,]\d+)?", value):
        tokens = [value]
    surfaces: list[float] = []
    for token in tokens:
        amount = _decimal(token)
        if amount is not None and amount > 0:
            surfaces.append(float(amount))
    return _unique(surfaces)


def parse_rooms(text: object | None, *, allow_bare: bool = False) -> int | None:
    """Parse a unique explicit room count (``2 pièces``, ``T2`` or ``F2``)."""

    value = normalize_text(text)
    if value is None:
        return None
    rooms = [
        int(match.group(1))
        for pattern in _ROOM_PATTERNS
        for match in pattern.finditer(value)
        if int(match.group(1)) > 0
    ]
    if allow_bare and not rooms and re.fullmatch(r"[1-9]\d?", value):
        rooms = [int(value)]
    return _unique(rooms)


def parse_postal_code(text: object | None) -> str | None:
    """Return a postal code only when one unique five-digit value is present."""

    value = normalize_text(text)
    if value is None:
        return None
    return _unique([match.group(1) for match in _POSTAL_RE.finditer(value)])


def parse_location(
    text: object | None,
    *,
    postal_code: str | None = None,
    allow_plain: bool = True,
) -> str | None:
    """Extract a conservative location from explicit text.

    Lyon arrondissement spellings are normalized because they are explicit in
    the source.  Otherwise only a standalone city-like line, optionally beside
    the supplied postal code, is accepted.  Long card prose is never treated as
    a location.
    """

    if text is None:
        return None
    raw = str(text)[:1_500]
    normalized = normalize_text(raw)
    if normalized is None:
        return None

    district = _LYON_DISTRICT_RE.search(normalized)
    if district:
        number = int(district.group(1))
        suffix = "er" if number == 1 else "e"
        return f"Lyon {number}{suffix}"

    if allow_plain:
        code = postal_code or parse_postal_code(normalized)
        for raw_segment in _LOCATION_SPLIT_RE.split(raw):
            segment = normalize_text(raw_segment)
            if segment is None:
                continue
            if code:
                segment = re.sub(
                    rf"(?<!\d){re.escape(code)}(?!\d)", " ", segment
                )
                segment = normalize_text(segment.strip(" ,-–—()[]"))
                if segment is None:
                    continue
            if _PLAIN_LOCATION_RE.fullmatch(segment) and not re.search(
                r"\b(?:appartement|maison|studio|pi[eè]ce|location)\b",
                segment,
                re.IGNORECASE,
            ):
                return segment

    # A bare mention of Lyon is useful but does not justify inventing an
    # arrondissement from the postal code.
    if re.search(r"\blyon\b", normalized, re.IGNORECASE):
        return "Lyon"
    return None


def _is_tracking_parameter(name: str) -> bool:
    lowered = name.casefold()
    return lowered.startswith("utm_") or lowered in TRACKING_PARAMETER_NAMES


def canonicalize_tracking_url(url: str, *, base_url: str | None = None) -> str:
    """Canonicalize an HTTP(S) URL without dropping identity parameters.

    Fragments and manifest tracking parameters are removed.  Remaining query
    pairs are sorted for deterministic deduplication and otherwise preserved.
    Invalid, credential-bearing or non-HTTP URLs return an empty string.
    """

    raw = str(url).strip()
    if not raw:
        return ""
    absolute = urljoin(base_url, raw) if base_url else raw
    try:
        parts = urlsplit(absolute)
        port = parts.port
    except ValueError:
        return ""
    scheme = parts.scheme.casefold()
    if scheme not in {"http", "https"} or not parts.hostname:
        return ""
    if parts.username is not None or parts.password is not None:
        return ""

    host = parts.hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    query_pairs = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_parameter(name)
    ]
    query = urlencode(sorted(query_pairs), doseq=True)
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


async def wait_for_detail_heading(page: Any) -> None:
    """Wait for hydrated detail data within Playwright's configured timeout.

    Real browser pages expose ``locator``. Lightweight offline test doubles
    may omit it because their semantic snapshot is immediately available. A
    real page without a non-empty visible ``h1`` is a detail parser failure,
    not a successful no-op enrichment.
    """

    locator_factory = getattr(page, "locator", None)
    if not callable(locator_factory):
        return
    try:
        heading = locator_factory("h1").first
        await heading.wait_for(state="visible")
        if normalize_text(await heading.inner_text()) is None:
            raise RuntimeError("empty detail heading")
    except Exception as exc:
        raise RuntimeError("detail page did not expose a hydrated h1") from exc


async def wait_for_first_result(locator: Any) -> None:
    """Give a JavaScript-rendered result list time to expose its first item.

    Playwright's page-level default selector timeout bounds the wait.  A
    timeout is intentionally swallowed: callers still inspect the resulting
    count and the runner classifies an unconfirmed empty snapshot as
    suspicious without mutating persisted state.  Lightweight fixture
    locators need not implement Playwright's ``first.wait_for`` API.
    """

    first = getattr(locator, "first", None)
    wait_for = getattr(first, "wait_for", None)
    if not callable(wait_for):
        return
    try:
        await wait_for(state="attached")
    except Exception:
        pass


@dataclass(frozen=True, slots=True)
class AdapterDiagnostics:
    """Compact parser counters suitable for the CLI diagnose command."""

    site: str
    loaded_url: str | None = None
    page_title: str | None = None
    candidate_links: int = 0
    valid_ids: int = 0
    listings: int = 0
    duplicates: int = 0
    rejected_candidates: int = 0

    def to_dict(self) -> dict[str, str | int | None]:
        return asdict(self)


class SiteAdapter(ABC):
    """Common extraction interface implemented independently by each site."""

    site_name: str
    search_url: str

    def __init__(self, search_url: str) -> None:
        # Search URLs are configured constants and must be loaded verbatim.  We
        # validate one without running detail-URL canonicalization over it.
        configured_url = str(search_url).strip()
        if not canonicalize_tracking_url(configured_url):
            raise ValueError("search_url must be an absolute HTTP(S) URL")
        self.search_url = configured_url
        self._diagnostics = AdapterDiagnostics(site=self.site_name)

    @property
    def diagnostics(self) -> AdapterDiagnostics:
        return self._diagnostics

    def diagnostic(self) -> dict[str, str | int | None]:
        """Return a JSON-serializable snapshot without page content."""

        return self._diagnostics.to_dict()

    @abstractmethod
    async def scan_results(self, page: Any) -> list[Listing]:
        """Extract and deduplicate the lightweight visible observations."""

        raise NotImplementedError

    @abstractmethod
    async def fetch_details(self, page: Any, listing: Listing) -> Listing:
        """Parse an already-loaded detail page and return an enriched value.

        Navigation and its bounded retry policy belong to ``BrowserSession``;
        adapters must not call ``page.goto`` from this method.
        """

        raise NotImplementedError

    @abstractmethod
    def extract_id(self, url: str) -> str | None:
        """Extract a source-provided identifier, never a synthetic one."""

        raise NotImplementedError

    @abstractmethod
    def canonicalize_url(self, url: str) -> str:
        """Return a stable detail URL or an empty string when unusable."""

        raise NotImplementedError

    @abstractmethod
    def parse_candidate(self, candidate: Candidate) -> Listing | None:
        """Normalize one compact, site-specific candidate snapshot."""

        raise NotImplementedError
