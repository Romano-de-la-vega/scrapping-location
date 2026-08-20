from dataclasses import replace

from watcher.diff import (
    changed_fields,
    compare_site,
    deduplicate_listings,
    material_fingerprint,
)
from watcher.models import EventType, Listing


NOW = "2026-08-20T08:00:00+02:00"
LATER = "2026-08-20T08:30:00+02:00"


def listing(identifier: str = "A", **overrides: object) -> Listing:
    values: dict[str, object] = {
        "site": "seloger",
        "listing_id": identifier,
        "canonical_url": f"https://example.test/{identifier}",
        "title": "Appartement",
        "price_eur": 750,
        "surface_m2": 35,
        "rooms": 2,
        "location": "Lyon 6e",
        "postal_code": "69006",
    }
    values.update(overrides)
    return Listing(**values)  # type: ignore[arg-type]


def baseline(*items: Listing):
    return compare_site(
        {},
        items,
        baseline_exists=False,
        site="seloger",
        previous_count=0,
        observed_at=NOW,
    )


def test_fingerprint_is_stable_and_ignores_cosmetic_title() -> None:
    original = listing()
    renamed = replace(original, title="Appartement lumineux")
    assert material_fingerprint(original) == material_fingerprint(renamed)
    assert material_fingerprint(original) != material_fingerprint(
        replace(original, price_eur=720)
    )


def test_changed_fields_returns_explicit_before_after() -> None:
    changes = changed_fields(listing(), listing(price_eur=720, rooms=3))
    assert changes["price_eur"].to_dict() == {"before": 750, "after": 720}
    assert changes["rooms"].to_dict() == {"before": 2, "after": 3}
    assert "title" not in changes


def test_duplicate_dom_id_is_merged_once() -> None:
    sparse = listing(price_eur=None, surface_m2=None)
    complete = listing(price_eur=750, surface_m2=35)
    result = deduplicate_listings([sparse, complete])
    assert len(result) == 1
    assert result[0].price_eur == 750
    assert result[0].surface_m2 == 35


def test_canonical_url_is_the_fallback_identity() -> None:
    first = listing(listing_id=None)
    duplicate = listing(listing_id=None, title="Other card copy")
    assert len(deduplicate_listings([first, duplicate])) == 1
    assert first.listing_id is None


def test_url_only_and_id_card_for_same_url_are_one_listing() -> None:
    without_id = listing(listing_id=None, price_eur=None)
    with_id = listing(listing_id="A", price_eur=750)
    result = deduplicate_listings([without_id, with_id])
    assert len(result) == 1
    assert result[0].listing_id == "A"
    assert result[0].price_eur == 750


def test_new_listing_after_baseline_emits_new() -> None:
    initial = baseline(listing("A"), listing("B"), listing("C"))
    result = compare_site(
        initial.state_updates,
        [listing("A"), listing("B"), listing("C"), listing("D")],
        baseline_exists=True,
        site="seloger",
        previous_count=3,
        observed_at=LATER,
    )
    assert [event.id for event in result.new] == ["D"]
    assert result.new[0].actionable is True


def test_material_price_change_emits_updated_with_before_after() -> None:
    initial = baseline(listing(price_eur=750))
    result = compare_site(
        initial.state_updates,
        [listing(price_eur=720)],
        baseline_exists=True,
        site="seloger",
        previous_count=1,
        observed_at=LATER,
    )
    assert len(result.events) == 1
    event = result.events[0]
    assert event.type is EventType.UPDATED
    assert event.changes["price_eur"].before == 750
    assert event.changes["price_eur"].after == 720
    assert event.before is not None and event.before.price_eur == 750
    assert event.after is not None and event.after.price_eur == 720


def test_rejected_listing_becoming_eligible_gets_special_event() -> None:
    initial = baseline(listing(price_eur=850))
    result = compare_site(
        initial.state_updates,
        [listing(price_eur=790)],
        baseline_exists=True,
        site="seloger",
        previous_count=1,
        observed_at=LATER,
    )
    assert len(result.events) == 1
    assert result.events[0].type is EventType.BECAME_ELIGIBLE
    assert result.events[0].actionable is True
    assert result.events[0].changes["price_eur"].to_dict() == {
        "before": 850,
        "after": 790,
    }


def test_cosmetic_title_change_is_persistable_but_not_an_event() -> None:
    initial = baseline(listing(title="Appartement"))
    result = compare_site(
        initial.state_updates,
        [listing(title="Appartement !")],
        baseline_exists=True,
        site="seloger",
        previous_count=1,
        observed_at=LATER,
    )
    assert result.events == ()
    assert result.state_updates[0].listing.title == "Appartement !"


def test_transient_unknown_value_does_not_erase_known_state() -> None:
    initial = baseline(listing(price_eur=750, surface_m2=35))
    result = compare_site(
        initial.state_updates,
        [listing(price_eur=None, surface_m2=None)],
        baseline_exists=True,
        site="seloger",
        previous_count=1,
        observed_at=LATER,
    )
    assert result.events == ()
    assert result.state_updates[0].listing.price_eur == 750
    assert result.state_updates[0].listing.surface_m2 == 35


def test_needs_detail_becoming_eligible_is_updated_not_special_event() -> None:
    initial = baseline(listing(surface_m2=None))
    result = compare_site(
        initial.state_updates,
        [listing(surface_m2=35)],
        baseline_exists=True,
        site="seloger",
        previous_count=1,
        observed_at=LATER,
    )
    assert len(result.events) == 1
    assert result.events[0].type is EventType.UPDATED


def test_url_only_identity_migration_keeps_known_values_without_false_event() -> None:
    url_only = listing(listing_id=None, price_eur=750, surface_m2=35)
    initial = baseline(url_only)
    sparse_with_id = listing(listing_id="A", price_eur=None, surface_m2=None)

    result = compare_site(
        initial.state_updates,
        [sparse_with_id],
        baseline_exists=True,
        site="seloger",
        previous_count=1,
        observed_at=LATER,
    )

    assert result.events == ()
    assert result.state_updates[0].listing.listing_id == "A"
    assert result.state_updates[0].listing.price_eur == 750
    assert result.state_updates[0].listing.surface_m2 == 35
    assert result.state_deletes == (url_only.identity_key,)
