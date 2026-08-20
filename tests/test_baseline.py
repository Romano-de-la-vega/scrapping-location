from watcher.database import Database
from watcher.models import Listing, SiteStatus


def listings(site: str, count: int) -> list[Listing]:
    return [
        Listing(
            site=site,
            listing_id=str(index),
            canonical_url=f"https://example.test/{site}/{index}",
            price_eur=750,
            surface_m2=35,
            postal_code="69006",
        )
        for index in range(count)
    ]


def test_first_successful_scan_creates_baseline_without_new_events(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        result = database.process_site_scan(
            "leboncoin", listings("leboncoin", 10), "run-1"
        )
        assert result.status is SiteStatus.BASELINE_CREATED
        assert result.baseline_created is True
        assert result.current_count == 10
        assert result.new == ()
        assert database.listing_count("leboncoin") == 10


def test_identical_second_scan_has_no_change(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        database.process_site_scan("leboncoin", listings("leboncoin", 3), "run-1")
        result = database.process_site_scan(
            "leboncoin", listings("leboncoin", 3), "run-2"
        )
        assert result.status is SiteStatus.OK
        assert result.events == ()
        assert all(state.seen_count == 2 for state in result.state_updates)


def test_baselines_are_independent_including_an_empty_baseline(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        first = database.process_site_scan("leboncoin", [], "run-1")
        database.failed_site_scan("seloger", reason="timeout")

        assert first.status is SiteStatus.BASELINE_CREATED
        assert database.has_site_baseline("leboncoin") is True
        assert database.get_site_state("leboncoin")["last_result_count"] == 0
        assert database.has_site_baseline("seloger") is False

        second = database.process_site_scan(
            "seloger", listings("seloger", 2), "run-2"
        )
        assert second.status is SiteStatus.BASELINE_CREATED
        assert second.events == ()


def test_dry_run_diff_does_not_create_a_baseline(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        result = database.process_site_scan(
            "seventee", listings("seventee", 1), "dry-run", dry_run=True
        )
        assert result.status is SiteStatus.BASELINE_CREATED
        assert database.has_site_baseline("seventee") is False
        assert database.listing_count("seventee") == 0

