"""Deterministic business eligibility rules."""

from __future__ import annotations

from watcher.config import CriteriaConfig
from watcher.models import EligibilityStatus, Listing


def evaluate_eligibility(
    listing: Listing,
    criteria: CriteriaConfig | None = None,
) -> EligibilityStatus:
    """Classify a listing without inferring any missing value.

    A known out-of-range field is enough to reject the listing.  Otherwise all
    three required values must be present before it can be declared eligible.
    This ordering avoids opening a detail page for an already-known rejection.
    """

    criteria = criteria or CriteriaConfig()

    if (
        listing.postal_code is not None
        and listing.postal_code not in criteria.postal_codes
    ):
        return EligibilityStatus.REJECTED
    if listing.price_eur is not None and not (
        criteria.price_min <= listing.price_eur <= criteria.price_max
    ):
        return EligibilityStatus.REJECTED
    if listing.surface_m2 is not None and not (
        criteria.surface_min <= listing.surface_m2 <= criteria.surface_max
    ):
        return EligibilityStatus.REJECTED

    if (
        listing.postal_code is None
        or listing.price_eur is None
        or listing.surface_m2 is None
    ):
        return EligibilityStatus.NEEDS_DETAIL

    return EligibilityStatus.ELIGIBLE

