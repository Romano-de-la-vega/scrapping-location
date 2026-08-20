"""Core data models shared by the watcher.

The module deliberately contains no scraping or persistence logic.  Values that
could not be extracted are represented by ``None``; model construction never
guesses missing real-estate data.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from math import isfinite
from typing import Any, Mapping


class EligibilityStatus(StrEnum):
    """Result of applying the configured deterministic criteria."""

    ELIGIBLE = "ELIGIBLE"
    REJECTED = "REJECTED"
    NEEDS_DETAIL = "NEEDS_DETAIL"


class EventType(StrEnum):
    """Persistent change types emitted by the diff engine."""

    NEW = "NEW"
    UPDATED = "UPDATED"
    BECAME_ELIGIBLE = "BECAME_ELIGIBLE"
    MISSING_FROM_SEARCH = "MISSING_FROM_SEARCH"


class SiteStatus(StrEnum):
    """Statuses produced by the deterministic site comparison."""

    OK = "OK"
    BASELINE_CREATED = "BASELINE_CREATED"
    SUSPICIOUS_RESULT = "SUSPICIOUS_RESULT"
    ERROR = "ERROR"
    CHALLENGE = "CHALLENGE"


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _optional_int(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer or None")
    integer = int(value)
    if integer != value:
        raise ValueError(f"{name} must be a whole number")
    return integer


@dataclass(frozen=True, slots=True)
class Listing:
    """A normalized listing as observed on either pass.

    ``listing_id`` may be absent when a site exposes only a stable detail URL.
    In that case :attr:`identity_key` uses the canonical URL, as required by the
    fallback deduplication rule.  At least one of the two values must exist.
    """

    site: str
    listing_id: str | None
    canonical_url: str | None
    title: str | None = None
    price_eur: int | None = None
    surface_m2: float | None = None
    rooms: int | None = None
    location: str | None = None
    postal_code: str | None = None
    eligibility: EligibilityStatus | str | None = None

    def __post_init__(self) -> None:
        site = str(self.site).strip().lower()
        listing_id = _optional_text(self.listing_id)
        canonical_url = _optional_text(self.canonical_url)
        if not site:
            raise ValueError("site must not be empty")
        if listing_id is None and canonical_url is None:
            raise ValueError("a listing needs a listing_id or a canonical_url")

        surface = self.surface_m2
        if surface is not None:
            if isinstance(surface, bool):
                raise TypeError("surface_m2 must be numeric or None")
            surface = float(surface)
            if not isfinite(surface):
                raise ValueError("surface_m2 must be finite")

        eligibility = self.eligibility
        if eligibility is not None and not isinstance(eligibility, EligibilityStatus):
            eligibility = EligibilityStatus(str(eligibility))

        object.__setattr__(self, "site", site)
        object.__setattr__(self, "listing_id", listing_id)
        object.__setattr__(self, "canonical_url", canonical_url)
        object.__setattr__(self, "title", _optional_text(self.title))
        object.__setattr__(self, "price_eur", _optional_int(self.price_eur, "price_eur"))
        object.__setattr__(self, "surface_m2", surface)
        object.__setattr__(self, "rooms", _optional_int(self.rooms, "rooms"))
        object.__setattr__(self, "location", _optional_text(self.location))
        object.__setattr__(self, "postal_code", _optional_text(self.postal_code))
        object.__setattr__(self, "eligibility", eligibility)

    @property
    def identity_key(self) -> str:
        """Internal stable key, preferring the source ID over the URL."""

        if self.listing_id is not None:
            return f"id:{self.listing_id}"
        # __post_init__ guarantees this is not None.
        return f"url:{self.canonical_url}"

    @property
    def id(self) -> str | None:
        """Output-friendly alias used by adapters and serializers."""

        return self.listing_id

    @property
    def url(self) -> str | None:
        return self.canonical_url

    def with_eligibility(self, value: EligibilityStatus) -> "Listing":
        return replace(self, eligibility=value)

    def to_dict(self) -> dict[str, Any]:
        """Return the normalized storage representation."""

        return {
            "site": self.site,
            "listing_id": self.listing_id,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "price_eur": self.price_eur,
            "surface_m2": self.surface_m2,
            "rooms": self.rooms,
            "location": self.location,
            "postal_code": self.postal_code,
            "eligibility": self.eligibility.value if self.eligibility else None,
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Return the compact names used by the final JSON protocol."""

        result = self.to_dict()
        result["id"] = result.pop("listing_id")
        result["url"] = result.pop("canonical_url")
        return result


# Search and detail passes intentionally share the exact normalized shape.
ListingSummary = Listing
ListingDetails = Listing


@dataclass(frozen=True, slots=True)
class ListingState:
    """A listing plus its database bookkeeping fields."""

    listing: Listing
    first_seen: str
    last_seen: str
    last_changed: str | None
    seen_count: int = 1
    missing_count: int = 0
    fingerprint: str | None = None

    @property
    def identity_key(self) -> str:
        return self.listing.identity_key


@dataclass(frozen=True, slots=True)
class FieldChange:
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {"before": self.before, "after": self.after}


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """A change with explicit snapshots and field-level before/after values."""

    event_type: EventType
    site: str
    listing_id: str | None
    canonical_url: str | None
    changes: Mapping[str, FieldChange] = field(default_factory=dict)
    before: Listing | None = None
    after: Listing | None = None

    @property
    def type(self) -> EventType:
        return self.event_type

    @property
    def id(self) -> str | None:
        return self.listing_id

    @property
    def actionable(self) -> bool:
        if self.event_type is EventType.BECAME_ELIGIBLE:
            return True
        if self.event_type is EventType.NEW and self.after is not None:
            return self.after.eligibility in {
                EligibilityStatus.ELIGIBLE,
                EligibilityStatus.NEEDS_DETAIL,
            }
        return False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.event_type.value,
            "site": self.site,
            "id": self.listing_id,
            "url": self.canonical_url,
            "changes": {
                name: change.to_dict() for name, change in self.changes.items()
            },
            "actionable": self.actionable,
        }
        if self.event_type is EventType.NEW and self.after is not None:
            details = self.after.to_public_dict()
            for name in (
                "title",
                "price_eur",
                "surface_m2",
                "rooms",
                "location",
                "postal_code",
                "eligibility",
            ):
                data[name] = details[name]
        return data


Event = ChangeEvent


@dataclass(frozen=True, slots=True)
class SiteDiff:
    """Pure comparison result and persistence plan for one site."""

    site: str
    status: SiteStatus
    baseline_created: bool
    suspicious: bool
    previous_count: int
    current_count: int
    events: tuple[ChangeEvent, ...] = ()
    state_updates: tuple[ListingState, ...] = field(default=(), repr=False)
    state_deletes: tuple[str, ...] = field(default=(), repr=False)
    current_listings: tuple[Listing, ...] = field(default=(), repr=False)
    reason: str | None = None

    @property
    def new(self) -> tuple[ChangeEvent, ...]:
        return tuple(event for event in self.events if event.type is EventType.NEW)

    @property
    def updated(self) -> tuple[ChangeEvent, ...]:
        return tuple(event for event in self.events if event.type is EventType.UPDATED)

    @property
    def became_eligible(self) -> tuple[ChangeEvent, ...]:
        return tuple(
            event for event in self.events if event.type is EventType.BECAME_ELIGIBLE
        )

    @property
    def missing_from_search(self) -> tuple[ChangeEvent, ...]:
        return tuple(
            event for event in self.events if event.type is EventType.MISSING_FROM_SEARCH
        )

    @property
    def actionable_count(self) -> int:
        return sum(event.actionable for event in self.events)
