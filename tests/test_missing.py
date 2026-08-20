from watcher.database import Database
from watcher.models import EventType, Listing, SiteStatus


def listing(identifier: str, *, site: str = "seloger") -> Listing:
    return Listing(
        site=site,
        listing_id=identifier,
        canonical_url=f"https://example.test/{site}/{identifier}",
        price_eur=750,
        surface_m2=35,
        postal_code="69006",
    )


def test_missing_event_is_emitted_only_when_threshold_is_crossed(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        database.process_site_scan("seloger", [listing("A"), listing("B")], "r1")

        first = database.process_site_scan("seloger", [listing("A")], "r2")
        assert first.missing_from_search == ()
        assert database.get_listing("seloger", "B").missing_count == 1

        second = database.process_site_scan("seloger", [listing("A")], "r3")
        assert len(second.missing_from_search) == 1
        assert second.missing_from_search[0].type is EventType.MISSING_FROM_SEARCH
        assert second.missing_from_search[0].actionable is False
        assert database.get_listing("seloger", "B").missing_count == 2

        third = database.process_site_scan("seloger", [listing("A")], "r4")
        assert third.missing_from_search == ()
        assert database.get_listing("seloger", "B").missing_count == 3


def test_reappearing_listing_resets_missing_and_can_cross_again(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        both = [listing("A"), listing("B")]
        database.process_site_scan("seloger", both, "r1")
        database.process_site_scan("seloger", [listing("A")], "r2")
        database.process_site_scan("seloger", [listing("A")], "r3")
        database.process_site_scan("seloger", both, "r4")
        assert database.get_listing("seloger", "B").missing_count == 0
        database.process_site_scan("seloger", [listing("A")], "r5")
        result = database.process_site_scan("seloger", [listing("A")], "r6")
        assert len(result.missing_from_search) == 1


def test_failed_site_scan_never_increments_missing(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        database.process_site_scan("seloger", [listing("A"), listing("B")], "r1")
        result = database.failed_site_scan("seloger", reason="captcha")
        assert result.status is SiteStatus.ERROR
        assert result.state_updates == ()
        assert database.get_listing("seloger", "A").missing_count == 0
        assert database.get_listing("seloger", "B").missing_count == 0


def test_empty_result_after_twenty_is_suspicious_and_mutates_nothing(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        initial = [listing(str(index)) for index in range(20)]
        database.process_site_scan("seloger", initial, "r1")
        result = database.process_site_scan("seloger", [], "r2")

        assert result.status is SiteStatus.SUSPICIOUS_RESULT
        assert result.suspicious is True
        assert result.previous_count == 20
        assert result.current_count == 0
        assert result.events == ()
        assert result.state_updates == ()
        assert database.get_site_state("seloger")["last_result_count"] == 20
        assert all(
            state.missing_count == 0
            for state in database.load_listing_states("seloger").values()
        )


def test_extreme_nonempty_drop_is_also_suspicious(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        initial = [listing(str(index)) for index in range(20)]
        database.process_site_scan("seloger", initial, "r1")
        result = database.process_site_scan(
            "seloger", initial[:4], "r2", suspicious_result_ratio=0.25
        )
        assert result.status is SiteStatus.SUSPICIOUS_RESULT
        assert database.get_site_state("seloger")["last_result_count"] == 20


def test_four_to_one_drop_is_quarantined_for_small_sites(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        initial = [listing(str(index), site="seventee") for index in range(4)]
        database.process_site_scan("seventee", initial, "r1")
        result = database.process_site_scan("seventee", initial[:1], "r2")

        assert result.status is SiteStatus.SUSPICIOUS_RESULT
        assert result.events == ()
        assert all(
            state.missing_count == 0
            for state in database.load_listing_states("seventee").values()
        )
