"""Transactional SQLite state store for deterministic watcher runs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from watcher.config import CriteriaConfig, DiffConfig
from watcher.diff import compare_site, failed_site_diff
from watcher.models import (
    ChangeEvent,
    EligibilityStatus,
    Listing,
    ListingState,
    SiteDiff,
    SiteStatus,
)


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS listings (
        site TEXT NOT NULL,
        identity_key TEXT NOT NULL,
        listing_id TEXT,
        canonical_url TEXT,
        title TEXT,
        price_eur INTEGER,
        surface_m2 REAL,
        rooms INTEGER,
        location TEXT,
        postal_code TEXT,
        eligibility TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        last_changed TEXT,
        seen_count INTEGER NOT NULL DEFAULT 1,
        missing_count INTEGER NOT NULL DEFAULT 0,
        fingerprint TEXT,
        PRIMARY KEY (site, identity_key),
        CHECK (listing_id IS NOT NULL OR canonical_url IS NOT NULL),
        CHECK (seen_count >= 1),
        CHECK (missing_count >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT,
        leboncoin_count INTEGER,
        seloger_count INTEGER,
        seventee_count INTEGER,
        new_count INTEGER DEFAULT 0,
        updated_count INTEGER DEFAULT 0,
        duration_ms INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        site TEXT NOT NULL,
        listing_id TEXT,
        event_type TEXT NOT NULL,
        before_json TEXT,
        after_json TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS site_state (
        site TEXT PRIMARY KEY,
        baseline_created_at TEXT NOT NULL,
        last_successful_scan TEXT NOT NULL,
        last_result_count INTEGER NOT NULL,
        CHECK (last_result_count >= 0)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_listings_site_missing ON listings(site, missing_count)",
)


def _now(value: str | datetime | None = None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _json(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class Database:
    """Small SQLite repository with explicit transaction boundaries.

    Constructing with ``dry_run=True`` opens an existing database read-only. If
    the path does not exist, it creates the schema only in ``:memory:``.  This
    guarantees that a dry-run cannot create or modify the configured DB file.
    """

    def __init__(self, path: str | Path = "data/state.db", *, dry_run: bool = False):
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self.dry_run = dry_run
        self.read_only = False
        self._savepoint_counter = 0
        self._ephemeral = False

        if dry_run and str(path) != ":memory:" and self.path.exists():
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            self.connection = sqlite3.connect(uri, uri=True, isolation_level=None)
            self.read_only = True
        else:
            database_target = ":memory:" if dry_run else str(path)
            self._ephemeral = dry_run
            if database_target != ":memory:":
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(database_target, isolation_level=None)

        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if not self.read_only:
            if not self._ephemeral and str(path) != ":memory:":
                self.connection.execute("PRAGMA journal_mode = WAL")
                self.connection.execute("PRAGMA synchronous = NORMAL")
            self.initialize()

    @property
    def conn(self) -> sqlite3.Connection:
        """Compatibility alias for low-level diagnostics and tests."""

        return self.connection

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Use ``BEGIN IMMEDIATE`` or a savepoint when already transactional."""

        if self.read_only or self.dry_run:
            raise RuntimeError("this database was opened for dry-run/read-only use")
        if self.connection.in_transaction:
            self._savepoint_counter += 1
            savepoint = f"watcher_sp_{self._savepoint_counter}"
            self.connection.execute(f"SAVEPOINT {savepoint}")
            try:
                yield self.connection
            except BaseException:
                self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            else:
                self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            return

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def initialize(self) -> None:
        """Create the idempotent schema atomically."""

        # An ephemeral dry-run database needs a schema, but no file is touched.
        temporarily_allow_memory = self.dry_run and self._ephemeral
        if self.read_only:
            return
        if temporarily_allow_memory:
            self.connection.execute("BEGIN")
            try:
                for statement in SCHEMA_STATEMENTS:
                    self.connection.execute(statement)
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")
            return
        with self.transaction() as connection:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)

    def _table_exists(self, table: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return row is not None

    def has_site_baseline(self, site: str) -> bool:
        if not self._table_exists("site_state"):
            return False
        row = self.connection.execute(
            "SELECT 1 FROM site_state WHERE site = ?", (site.strip().lower(),)
        ).fetchone()
        return row is not None

    def get_site_state(self, site: str) -> dict[str, Any] | None:
        if not self._table_exists("site_state"):
            return None
        row = self.connection.execute(
            """
            SELECT site, baseline_created_at, last_successful_scan, last_result_count
            FROM site_state WHERE site = ?
            """,
            (site.strip().lower(),),
        ).fetchone()
        return dict(row) if row else None

    def _row_to_state(self, row: sqlite3.Row) -> ListingState:
        eligibility = row["eligibility"]
        listing = Listing(
            site=row["site"],
            listing_id=row["listing_id"],
            canonical_url=row["canonical_url"],
            title=row["title"],
            price_eur=row["price_eur"],
            surface_m2=row["surface_m2"],
            rooms=row["rooms"],
            location=row["location"],
            postal_code=row["postal_code"],
            eligibility=EligibilityStatus(eligibility) if eligibility else None,
        )
        return ListingState(
            listing=listing,
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            last_changed=row["last_changed"],
            seen_count=row["seen_count"],
            missing_count=row["missing_count"],
            fingerprint=row["fingerprint"],
        )

    def load_listing_states(self, site: str) -> dict[str, ListingState]:
        if not self._table_exists("listings"):
            return {}
        rows = self.connection.execute(
            "SELECT * FROM listings WHERE site = ? ORDER BY identity_key",
            (site.strip().lower(),),
        ).fetchall()
        return {row["identity_key"]: self._row_to_state(row) for row in rows}

    def load_listings(self, site: str) -> list[Listing]:
        return [state.listing for state in self.load_listing_states(site).values()]

    def get_listing(self, site: str, listing_id: str) -> ListingState | None:
        if not self._table_exists("listings"):
            return None
        row = self.connection.execute(
            "SELECT * FROM listings WHERE site = ? AND listing_id = ?",
            (site.strip().lower(), str(listing_id)),
        ).fetchone()
        return self._row_to_state(row) if row else None

    def listing_count(self, site: str | None = None) -> int:
        if not self._table_exists("listings"):
            return 0
        if site is None:
            row = self.connection.execute("SELECT COUNT(*) AS count FROM listings").fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM listings WHERE site = ?",
                (site.strip().lower(),),
            ).fetchone()
        return int(row["count"])

    def _upsert_state(self, connection: sqlite3.Connection, state: ListingState) -> None:
        listing = state.listing
        connection.execute(
            """
            INSERT INTO listings (
                site, identity_key, listing_id, canonical_url, title, price_eur,
                surface_m2, rooms, location, postal_code, eligibility,
                first_seen, last_seen, last_changed, seen_count, missing_count,
                fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(site, identity_key) DO UPDATE SET
                listing_id = excluded.listing_id,
                canonical_url = excluded.canonical_url,
                title = excluded.title,
                price_eur = excluded.price_eur,
                surface_m2 = excluded.surface_m2,
                rooms = excluded.rooms,
                location = excluded.location,
                postal_code = excluded.postal_code,
                eligibility = excluded.eligibility,
                first_seen = excluded.first_seen,
                last_seen = excluded.last_seen,
                last_changed = excluded.last_changed,
                seen_count = excluded.seen_count,
                missing_count = excluded.missing_count,
                fingerprint = excluded.fingerprint
            """,
            (
                listing.site,
                listing.identity_key,
                listing.listing_id,
                listing.canonical_url,
                listing.title,
                listing.price_eur,
                listing.surface_m2,
                listing.rooms,
                listing.location,
                listing.postal_code,
                listing.eligibility.value if listing.eligibility else None,
                state.first_seen,
                state.last_seen,
                state.last_changed,
                state.seen_count,
                state.missing_count,
                state.fingerprint,
            ),
        )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event: ChangeEvent,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO events (
                run_id, site, listing_id, event_type,
                before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                event.site,
                event.listing_id,
                event.event_type.value,
                _json(event.before.to_dict() if event.before else None),
                _json(event.after.to_dict() if event.after else None),
                created_at,
            ),
        )

    def process_site_scan(
        self,
        site: str,
        listings: Iterable[Listing],
        run_id: str | None = None,
        *,
        criteria: CriteriaConfig | None = None,
        diff_config: DiffConfig | None = None,
        missing_threshold: int | None = None,
        suspicious_result_ratio: float | None = None,
        suspicious_min_previous_count: int | None = None,
        observed_at: str | datetime | None = None,
        dry_run: bool = False,
    ) -> SiteDiff:
        """Compare and, unless dry-run/suspicious, atomically persist one site."""

        site_name = site.strip().lower()
        if not site_name:
            raise ValueError("site must not be empty")
        current = tuple(listings)
        moment = _now(observed_at)
        diff_config = diff_config or DiffConfig()
        threshold = (
            diff_config.missing_threshold
            if missing_threshold is None
            else missing_threshold
        )
        ratio = (
            diff_config.suspicious_result_ratio
            if suspicious_result_ratio is None
            else suspicious_result_ratio
        )
        minimum = (
            diff_config.suspicious_min_previous_count
            if suspicious_min_previous_count is None
            else suspicious_min_previous_count
        )

        def build_diff() -> SiteDiff:
            site_metadata = self.get_site_state(site_name)
            previous = self.load_listing_states(site_name)
            return compare_site(
                previous,
                current,
                baseline_exists=site_metadata is not None,
                site=site_name,
                previous_count=(
                    int(site_metadata["last_result_count"])
                    if site_metadata is not None
                    else 0
                ),
                criteria=criteria,
                missing_threshold=threshold,
                suspicious_result_ratio=ratio,
                suspicious_min_previous_count=minimum,
                observed_at=moment,
            )

        effective_dry_run = dry_run or self.dry_run or self.read_only
        if effective_dry_run:
            return build_diff()

        with self.transaction() as connection:
            result = build_diff()
            if result.suspicious:
                return result
            for identity_key in result.state_deletes:
                connection.execute(
                    "DELETE FROM listings WHERE site = ? AND identity_key = ?",
                    (site_name, identity_key),
                )
            for state in result.state_updates:
                self._upsert_state(connection, state)
            connection.execute(
                """
                INSERT INTO site_state (
                    site, baseline_created_at, last_successful_scan, last_result_count
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(site) DO UPDATE SET
                    last_successful_scan = excluded.last_successful_scan,
                    last_result_count = excluded.last_result_count
                """,
                (site_name, moment, moment, result.current_count),
            )
            event_run_id = run_id or moment
            for event in result.events:
                self._insert_event(connection, event_run_id, event, moment)
            return result

    # Short alias used by integrations which treat a successful scan as a unit.
    process_scan = process_site_scan

    def failed_site_scan(
        self,
        site: str,
        *,
        status: SiteStatus = SiteStatus.ERROR,
        reason: str | None = None,
    ) -> SiteDiff:
        """Return an error result without modifying any listing or site state."""

        metadata = self.get_site_state(site)
        previous_count = int(metadata["last_result_count"]) if metadata else 0
        return failed_site_diff(
            site,
            status=status,
            previous_count=previous_count,
            reason=reason,
        )

    def start_run(
        self,
        run_id: str,
        *,
        started_at: str | datetime | None = None,
        dry_run: bool = False,
    ) -> None:
        if dry_run or self.dry_run or self.read_only:
            return
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO runs (run_id, started_at, status) VALUES (?, ?, ?)",
                (run_id, _now(started_at), "RUNNING"),
            )

    begin_run = start_run

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        site_counts: Mapping[str, int | None] | None = None,
        new_count: int = 0,
        updated_count: int = 0,
        duration_ms: int | None = None,
        finished_at: str | datetime | None = None,
        started_at: str | datetime | None = None,
        dry_run: bool = False,
    ) -> None:
        if dry_run or self.dry_run or self.read_only:
            return
        counts = site_counts or {}
        finished = _now(finished_at)
        started = _now(started_at) if started_at is not None else finished
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, started_at, finished_at, status,
                    leboncoin_count, seloger_count, seventee_count,
                    new_count, updated_count, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    finished_at = excluded.finished_at,
                    status = excluded.status,
                    leboncoin_count = excluded.leboncoin_count,
                    seloger_count = excluded.seloger_count,
                    seventee_count = excluded.seventee_count,
                    new_count = excluded.new_count,
                    updated_count = excluded.updated_count,
                    duration_ms = excluded.duration_ms
                """,
                (
                    run_id,
                    started,
                    finished,
                    str(status),
                    counts.get("leboncoin"),
                    counts.get("seloger"),
                    counts.get("seventee"),
                    int(new_count),
                    int(updated_count),
                    duration_ms,
                ),
            )

    def get_recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        if not self._table_exists("events"):
            return []
        rows = self.connection.execute(
            """
            SELECT id, run_id, site, listing_id, event_type,
                   before_json, after_json, created_at
            FROM events ORDER BY id DESC LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "site": row["site"],
                "listing_id": row["listing_id"],
                "event_type": row["event_type"],
                "before": json.loads(row["before_json"])
                if row["before_json"]
                else None,
                "after": json.loads(row["after_json"])
                if row["after_json"]
                else None,
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    history = get_recent_events

    def get_recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or not self._table_exists("runs"):
            return []
        rows = self.connection.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_status(self) -> dict[str, Any]:
        sites: dict[str, Any] = {}
        if self._table_exists("site_state"):
            rows = self.connection.execute(
                """
                SELECT s.site, s.baseline_created_at, s.last_successful_scan,
                       s.last_result_count, COUNT(l.identity_key) AS stored_count,
                       COALESCE(SUM(CASE WHEN l.missing_count > 0 THEN 1 ELSE 0 END), 0)
                           AS currently_missing_count
                FROM site_state AS s
                LEFT JOIN listings AS l ON l.site = s.site
                GROUP BY s.site
                ORDER BY s.site
                """
            ).fetchall()
            sites = {row["site"]: dict(row) for row in rows}
        recent_runs = self.get_recent_runs(1)
        return {"sites": sites, "last_run": recent_runs[0] if recent_runs else None}


StateDatabase = Database


def open_database(
    path: str | Path = "data/state.db", *, dry_run: bool = False
) -> Database:
    """Open the persistent store, or a safe read-only/memory dry-run view."""

    return Database(path, dry_run=dry_run)
