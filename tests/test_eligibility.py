from watcher.config import CriteriaConfig
from watcher.eligibility import evaluate_eligibility
from watcher.models import EligibilityStatus, Listing


def listing(**overrides: object) -> Listing:
    values: dict[str, object] = {
        "site": "seloger",
        "listing_id": "abc",
        "canonical_url": "https://example.test/abc",
        "price_eur": 750,
        "surface_m2": 35,
        "postal_code": "69006",
    }
    values.update(overrides)
    return Listing(**values)  # type: ignore[arg-type]


def test_matching_listing_is_eligible() -> None:
    assert evaluate_eligibility(listing()) is EligibilityStatus.ELIGIBLE


def test_limits_are_inclusive() -> None:
    assert evaluate_eligibility(listing(price_eur=550, surface_m2=30)) == "ELIGIBLE"
    assert evaluate_eligibility(listing(price_eur=800, surface_m2=60)) == "ELIGIBLE"


def test_each_known_out_of_range_value_rejects() -> None:
    assert evaluate_eligibility(listing(postal_code="69003")) == "REJECTED"
    assert evaluate_eligibility(listing(price_eur=801)) == "REJECTED"
    assert evaluate_eligibility(listing(surface_m2=29.9)) == "REJECTED"


def test_known_rejection_wins_over_unknown_values() -> None:
    candidate = listing(postal_code="69003", price_eur=None, surface_m2=None)
    assert evaluate_eligibility(candidate) is EligibilityStatus.REJECTED


def test_unknown_required_value_needs_detail_without_mutating_listing() -> None:
    candidate = listing(price_eur=None)
    assert evaluate_eligibility(candidate) is EligibilityStatus.NEEDS_DETAIL
    assert candidate.price_eur is None


def test_custom_criteria_are_used() -> None:
    criteria = CriteriaConfig(
        postal_codes=("69003",),
        price_min=400,
        price_max=900,
        surface_min=20,
        surface_max=70,
    )
    assert evaluate_eligibility(
        listing(postal_code="69003", price_eur=850, surface_m2=65), criteria
    ) == "ELIGIBLE"

