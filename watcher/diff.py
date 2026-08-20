"""Pure, deterministic comparison engine.

No function in this module writes to SQLite.  :func:`compare_site` returns a
complete :class:`~watcher.models.SiteDiff` plan which can either be persisted or
reported unchanged for a dry-run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

from watcher.config import CriteriaConfig
from watcher.eligibility import evaluate_eligibility
from watcher.models import (
    ChangeEvent,
    EligibilityStatus,
    EventType,
    FieldChange,
    Listing,
    ListingState,
    SiteDiff,
    SiteStatus,
)


MATERIAL_FIELDS: tuple[str, ...] = (
    "price_eur",
    "surface_m2",
    "rooms",
    "location",
    "postal_code",
    "canonical_url",
)


def _timestamp(value: str | datetime | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def material_payload(listing: Listing) -> dict[str, object]:
    """Return only fields whose changes are material to the watcher."""

    return {name: getattr(listing, name) for name in MATERIAL_FIELDS}


def material_fingerprint(listing: Listing) -> str:
    """Compute a stable SHA-256 over canonical JSON material fields."""

    payload = json.dumps(
        material_payload(listing),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


fingerprint_listing = material_fingerprint


def changed_fields(
    before: Listing,
    after: Listing,
    fields: Sequence[str] = MATERIAL_FIELDS,
) -> dict[str, FieldChange]:
    """Return deterministic field-level before/after values."""

    changes: dict[str, FieldChange] = {}
    for name in fields:
        previous = getattr(before, name)
        current = getattr(after, name)
        if previous != current:
            changes[name] = FieldChange(previous, current)
    return changes


def merge_observations(previous: Listing, current: Listing) -> Listing:
    """Keep previously known values when a later compact scan omits them.

    A missing value is not evidence that the real-world field was removed.  It
    is therefore unsafe to erase known state or emit a change solely because a
    result card became less detailed after a site redesign.
    """

    if previous.site != current.site:
        raise ValueError("observations must belong to the same site")
    values: dict[str, object] = {}
    for name in (
        "listing_id",
        "canonical_url",
        "title",
        "price_eur",
        "surface_m2",
        "rooms",
        "location",
        "postal_code",
    ):
        current_value = getattr(current, name)
        values[name] = (
            current_value if current_value is not None else getattr(previous, name)
        )
    values["eligibility"] = None
    return replace(current, **values)


def _merge_duplicate(first: Listing, duplicate: Listing) -> Listing:
    """Fill unknown fields from a duplicate card while keeping first conflicts."""

    values: dict[str, object] = {}
    for name in (
        "listing_id",
        "canonical_url",
        "title",
        "price_eur",
        "surface_m2",
        "rooms",
        "location",
        "postal_code",
        "eligibility",
    ):
        first_value = getattr(first, name)
        values[name] = first_value if first_value is not None else getattr(duplicate, name)
    return replace(first, **values)


def deduplicate_listings(listings: Iterable[Listing]) -> tuple[Listing, ...]:
    """Deduplicate DOM cards by source ID, or canonical URL as fallback."""

    merged: list[Listing] = []
    id_index: dict[tuple[str, str], int] = {}
    # None means that the URL was observed with conflicting source IDs, so a
    # URL-only card must not be guessed to belong to either one.
    url_index: dict[tuple[str, str], int | None] = {}
    for listing in listings:
        index: int | None = None
        id_key = (
            (listing.site, listing.listing_id)
            if listing.listing_id is not None
            else None
        )
        url_key = (
            (listing.site, listing.canonical_url)
            if listing.canonical_url is not None
            else None
        )
        if id_key is not None:
            index = id_index.get(id_key)
        if index is None and url_key is not None:
            candidate_index = url_index.get(url_key)
            if candidate_index is not None:
                candidate = merged[candidate_index]
                if (
                    candidate.listing_id is None
                    or listing.listing_id is None
                    or candidate.listing_id == listing.listing_id
                ):
                    index = candidate_index

        if index is None:
            index = len(merged)
            merged.append(listing)
        else:
            merged[index] = _merge_duplicate(merged[index], listing)

        current = merged[index]
        if current.listing_id is not None:
            id_index[(current.site, current.listing_id)] = index
        if current.canonical_url is not None:
            current_url_key = (current.site, current.canonical_url)
            existing_index = url_index.get(current_url_key)
            if existing_index is None and current_url_key in url_index:
                pass
            elif existing_index is not None and existing_index != index:
                other = merged[existing_index]
                if (
                    other.listing_id is not None
                    and current.listing_id is not None
                    and other.listing_id != current.listing_id
                ):
                    url_index[current_url_key] = None
                else:
                    url_index[current_url_key] = index
            else:
                url_index[current_url_key] = index
    return tuple(merged)


def is_suspicious_result(
    previous_count: int,
    current_count: int,
    *,
    ratio: float = 0.25,
    min_previous_count: int = 2,
) -> bool:
    """Detect empty or extremely abrupt result-count collapses."""

    if previous_count <= 0:
        return False
    if current_count == 0:
        return True
    return (
        previous_count >= min_previous_count
        and current_count / previous_count <= ratio
    )


def _previous_mapping(
    previous: Mapping[str, ListingState] | Iterable[ListingState],
) -> dict[str, ListingState]:
    if isinstance(previous, Mapping):
        return dict(previous)
    return {state.identity_key: state for state in previous}


def _infer_site(
    site: str | None,
    previous: Mapping[str, ListingState],
    current: Sequence[Listing],
) -> str:
    candidates = {listing.site for listing in current}
    candidates.update(state.listing.site for state in previous.values())
    if site is not None:
        normalized = site.strip().lower()
        if not normalized:
            raise ValueError("site must not be empty")
        if candidates and candidates != {normalized}:
            raise ValueError("all listings and previous states must match site")
        return normalized
    if len(candidates) != 1:
        raise ValueError("site is required when it cannot be inferred uniquely")
    return candidates.pop()


def compare_site(
    previous: Mapping[str, ListingState] | Iterable[ListingState],
    current: Iterable[Listing],
    *,
    baseline_exists: bool,
    site: str | None = None,
    previous_count: int | None = None,
    criteria: CriteriaConfig | None = None,
    missing_threshold: int = 2,
    suspicious_result_ratio: float = 0.25,
    suspicious_min_previous_count: int = 2,
    observed_at: str | datetime | None = None,
) -> SiteDiff:
    """Compare one successful site scan and return its complete state plan.

    A suspicious scan returns no events and no state updates.  Thus persisting
    the returned plan cannot accidentally increment ``missing_count``.
    """

    if missing_threshold < 1:
        raise ValueError("missing_threshold must be >= 1")
    if not 0.0 <= suspicious_result_ratio <= 1.0:
        raise ValueError("suspicious_result_ratio must be between 0 and 1")

    previous_by_key = _previous_mapping(previous)
    deduplicated = deduplicate_listings(current)
    site_name = _infer_site(site, previous_by_key, deduplicated)
    criteria = criteria or CriteriaConfig()
    previous_by_url: dict[str, ListingState | None] = {}
    for state in previous_by_key.values():
        url = state.listing.canonical_url
        if url is None:
            continue
        if url in previous_by_url:
            previous_by_url[url] = None
        else:
            previous_by_url[url] = state
    current_url_counts: dict[str, int] = {}
    for listing in deduplicated:
        if listing.canonical_url is not None:
            current_url_counts[listing.canonical_url] = (
                current_url_counts.get(listing.canonical_url, 0) + 1
            )

    classified_items: list[Listing] = []
    for listing in deduplicated:
        old_state = previous_by_key.get(listing.identity_key)
        if (
            old_state is None
            and listing.canonical_url is not None
            and current_url_counts.get(listing.canonical_url) == 1
        ):
            candidate = previous_by_url.get(listing.canonical_url)
            if candidate is not None and (
                candidate.listing.listing_id is None
                or listing.listing_id is None
                or candidate.listing.listing_id == listing.listing_id
            ):
                old_state = candidate
        complete = (
            merge_observations(old_state.listing, listing)
            if old_state is not None
            else listing
        )
        classified_items.append(
            complete.with_eligibility(evaluate_eligibility(complete, criteria))
        )
    classified = tuple(classified_items)
    prior_count = len(previous_by_key) if previous_count is None else previous_count
    current_count = len(classified)
    now = _timestamp(observed_at)

    if baseline_exists and is_suspicious_result(
        prior_count,
        current_count,
        ratio=suspicious_result_ratio,
        min_previous_count=suspicious_min_previous_count,
    ):
        return SiteDiff(
            site=site_name,
            status=SiteStatus.SUSPICIOUS_RESULT,
            baseline_created=False,
            suspicious=True,
            previous_count=prior_count,
            current_count=current_count,
            current_listings=classified,
            reason="result count dropped below the configured safety threshold",
        )

    if not baseline_exists:
        states = tuple(
            ListingState(
                listing=listing,
                first_seen=now,
                last_seen=now,
                last_changed=None,
                seen_count=1,
                missing_count=0,
                fingerprint=material_fingerprint(listing),
            )
            for listing in classified
        )
        return SiteDiff(
            site=site_name,
            status=SiteStatus.BASELINE_CREATED,
            baseline_created=True,
            suspicious=False,
            previous_count=prior_count,
            current_count=current_count,
            events=(),
            state_updates=states,
            current_listings=classified,
        )

    events: list[ChangeEvent] = []
    updates: list[ListingState] = []
    deletes: list[str] = []
    seen_keys: set[str] = set()

    for original_listing in classified:
        listing = original_listing
        key = listing.identity_key
        old_state = previous_by_key.get(key)
        if (
            old_state is None
            and listing.canonical_url is not None
            and current_url_counts.get(listing.canonical_url) == 1
        ):
            candidate = previous_by_url.get(listing.canonical_url)
            if candidate is not None and (
                candidate.listing.listing_id is None
                or listing.listing_id is None
                or candidate.listing.listing_id == listing.listing_id
            ):
                old_state = candidate
                # A temporarily absent source ID can be recovered from the
                # prior observation; this is stored evidence, not fabrication.
                if listing.listing_id is None:
                    listing = replace(
                        listing,
                        listing_id=candidate.listing.listing_id,
                    )
                    key = listing.identity_key
                if candidate.identity_key != key:
                    deletes.append(candidate.identity_key)
        seen_keys.add(old_state.identity_key if old_state is not None else key)
        fingerprint = material_fingerprint(listing)
        if old_state is None:
            updates.append(
                ListingState(
                    listing=listing,
                    first_seen=now,
                    last_seen=now,
                    last_changed=None,
                    seen_count=1,
                    missing_count=0,
                    fingerprint=fingerprint,
                )
            )
            events.append(
                ChangeEvent(
                    event_type=EventType.NEW,
                    site=site_name,
                    listing_id=listing.listing_id,
                    canonical_url=listing.canonical_url,
                    before=None,
                    after=listing,
                )
            )
            continue

        before = old_state.listing
        changes = changed_fields(before, listing)
        old_eligibility = before.eligibility or evaluate_eligibility(before, criteria)
        eligibility_changed = old_eligibility != listing.eligibility
        became_eligible = (
            old_eligibility is EligibilityStatus.REJECTED
            and listing.eligibility is EligibilityStatus.ELIGIBLE
        )
        last_changed = (
            now if changes or eligibility_changed else old_state.last_changed
        )
        updates.append(
            ListingState(
                listing=listing,
                first_seen=old_state.first_seen,
                last_seen=now,
                last_changed=last_changed,
                seen_count=old_state.seen_count + 1,
                missing_count=0,
                fingerprint=fingerprint,
            )
        )

        if became_eligible:
            event_changes = dict(changes)
            if not event_changes and eligibility_changed:
                event_changes["eligibility"] = FieldChange(
                    old_eligibility.value,
                    listing.eligibility.value,
                )
            events.append(
                ChangeEvent(
                    event_type=EventType.BECAME_ELIGIBLE,
                    site=site_name,
                    listing_id=listing.listing_id,
                    canonical_url=listing.canonical_url,
                    changes=event_changes,
                    before=before,
                    after=listing,
                )
            )
        elif changes:
            events.append(
                ChangeEvent(
                    event_type=EventType.UPDATED,
                    site=site_name,
                    listing_id=listing.listing_id,
                    canonical_url=listing.canonical_url,
                    changes=changes,
                    before=before,
                    after=listing,
                )
            )

    for key in sorted(previous_by_key.keys() - seen_keys):
        old_state = previous_by_key[key]
        next_missing_count = old_state.missing_count + 1
        updates.append(
            replace(old_state, missing_count=next_missing_count)
        )
        if old_state.missing_count < missing_threshold <= next_missing_count:
            listing = old_state.listing
            events.append(
                ChangeEvent(
                    event_type=EventType.MISSING_FROM_SEARCH,
                    site=site_name,
                    listing_id=listing.listing_id,
                    canonical_url=listing.canonical_url,
                    changes={
                        "missing_count": FieldChange(
                            old_state.missing_count,
                            next_missing_count,
                        )
                    },
                    before=listing,
                    after=listing,
                )
            )

    return SiteDiff(
        site=site_name,
        status=SiteStatus.OK,
        baseline_created=False,
        suspicious=False,
        previous_count=prior_count,
        current_count=current_count,
        events=tuple(events),
        state_updates=tuple(updates),
        state_deletes=tuple(dict.fromkeys(deletes)),
        current_listings=classified,
    )


compare_with_previous_state = compare_site


def failed_site_diff(
    site: str,
    *,
    status: SiteStatus = SiteStatus.ERROR,
    previous_count: int = 0,
    reason: str | None = None,
) -> SiteDiff:
    """Represent a failed scan explicitly; it never contains state updates."""

    if status not in {SiteStatus.ERROR, SiteStatus.CHALLENGE}:
        raise ValueError("failed site status must be ERROR or CHALLENGE")
    return SiteDiff(
        site=site.strip().lower(),
        status=status,
        baseline_created=False,
        suspicious=False,
        previous_count=previous_count,
        current_count=0,
        reason=reason,
    )
