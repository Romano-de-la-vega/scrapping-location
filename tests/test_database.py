import sqlite3

import pytest

from watcher.database import Database, open_database
from watcher.models import EventType, Listing


def listing(
    identifier: str = "A", *, price: int = 750, listing_id: str | None = "A"
) -> Listing:
    return Listing(
        site="seloger",
        listing_id=listing_id,
        canonical_url=f"https://example.test/{identifier}",
        title="Appartement",
        price_eur=price,
        surface_m2=35,
        rooms=2,
        location="Lyon 6e",
        postal_code="69006",
    )


def test_schema_is_initialized_with_required_and_site_state_tables(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        tables = {
            row["name"]
            for row in database.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"listings", "runs", "events", "site_state"} <= tables
        columns = {
            row["name"] for row in database.conn.execute("PRAGMA table_info(listings)")
        }
        assert {
            "site",
            "identity_key",
            "listing_id",
            "canonical_url",
            "first_seen",
            "last_seen",
            "seen_count",
            "missing_count",
            "fingerprint",
        } <= columns


def test_transaction_rolls_back_all_writes_on_error(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        with pytest.raises(RuntimeError):
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO runs (run_id, started_at) VALUES ('failed', 'now')"
                )
                raise RuntimeError("abort")
        assert database.get_recent_runs() == []


def test_run_lifecycle_and_event_history_keep_before_after(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        database.start_run("r1", started_at="2026-08-20T08:00:00+02:00")
        database.process_site_scan("seloger", [listing(price=750)], "r1")
        database.finish_run(
            "r1",
            status="BASELINE_CREATED",
            site_counts={"seloger": 1},
            duration_ms=12,
            finished_at="2026-08-20T08:00:01+02:00",
        )
        database.start_run("r2", started_at="2026-08-20T08:30:00+02:00")
        result = database.process_site_scan("seloger", [listing(price=720)], "r2")
        database.finish_run(
            "r2",
            status="CHANGES",
            site_counts={"seloger": 1},
            updated_count=1,
            duration_ms=10,
            finished_at="2026-08-20T08:30:01+02:00",
        )

        assert result.events[0].type is EventType.UPDATED
        history = database.get_recent_events(20)
        assert len(history) == 1
        assert history[0]["event_type"] == "UPDATED"
        assert history[0]["before"]["price_eur"] == 750
        assert history[0]["after"]["price_eur"] == 720
        status = database.get_status()
        assert status["last_run"]["run_id"] == "r2"
        assert status["sites"]["seloger"]["stored_count"] == 1


def test_url_fallback_does_not_fabricate_a_listing_id(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        candidate = listing(identifier="url-only", listing_id=None)
        database.process_site_scan("seloger", [candidate], "r1")
        state = next(iter(database.load_listing_states("seloger").values()))
        assert state.listing.listing_id is None
        assert state.identity_key == "url:https://example.test/url-only"


def test_later_observed_id_upgrades_url_identity_without_new_event(tmp_path) -> None:
    with Database(tmp_path / "state.db") as database:
        url_only = listing(identifier="same", listing_id=None)
        database.process_site_scan("seloger", [url_only], "r1")
        with_id = listing(identifier="same", listing_id="real-id")
        result = database.process_site_scan("seloger", [with_id], "r2")
        states = database.load_listing_states("seloger")
        assert result.events == ()
        assert list(states) == ["id:real-id"]
        assert states["id:real-id"].listing.listing_id == "real-id"


def test_opening_absent_database_for_dry_run_never_creates_a_file(tmp_path) -> None:
    path = tmp_path / "nested" / "state.db"
    with open_database(path, dry_run=True) as database:
        result = database.process_site_scan("seloger", [listing()], "dry")
        assert result.baseline_created is True
        database.start_run("ignored")
        database.finish_run("ignored", status="BASELINE_CREATED")
    assert not path.exists()
    assert not path.parent.exists()


def test_existing_database_is_read_only_during_dry_run(tmp_path) -> None:
    path = tmp_path / "state.db"
    with Database(path) as database:
        database.process_site_scan("seloger", [listing()], "r1")
    with open_database(path, dry_run=True) as database:
        preview = database.process_site_scan(
            "seloger", [listing(identifier="B", listing_id="B")], "dry"
        )
        assert [event.id for event in preview.new] == ["B"]
        with pytest.raises(sqlite3.OperationalError):
            database.conn.execute("DELETE FROM listings")
    with Database(path) as database:
        assert database.listing_count("seloger") == 1
