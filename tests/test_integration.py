from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from watcher.config import AppConfig, config_from_dict
from watcher.database import Database
from watcher.models import Listing
from watcher.runner import run_watcher


SITES = ("leboncoin", "seloger", "seventee")


def listing(
    site: str,
    identifier: str,
    *,
    price: int | None = 750,
    surface: float | None = 35,
    title: str | None = "Appartement",
) -> Listing:
    return Listing(
        site=site,
        listing_id=identifier,
        canonical_url=f"https://example.test/{site}/{identifier}",
        title=title,
        price_eur=price,
        surface_m2=surface,
        rooms=2,
        location="Lyon 6e",
        postal_code="69006",
    )


def config(tmp_path: Path, *sites: str, database_path: Path | None = None) -> AppConfig:
    selected = sites or ("seloger",)
    return config_from_dict(
        {
            "database_path": str(database_path or tmp_path / "state.db"),
            "browser": {"headless": True},
            "sites": {
                site: {"search_url": f"https://search.test/{site}"}
                for site in selected
            },
        },
        base_dir=tmp_path,
    )


@dataclass
class FakeWorld:
    search: dict[str, list[Listing] | Exception] = field(default_factory=dict)
    details: dict[tuple[str, str], Listing | Exception] = field(default_factory=dict)
    challenges: set[str] = field(default_factory=set)
    explicit_empty: set[str] = field(default_factory=set)
    redirects: dict[str, str] = field(default_factory=dict)
    search_calls: list[str] = field(default_factory=list)
    detail_calls: list[tuple[str, str]] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)


class FakePage:
    url = "about:blank"


class FakeBrowserSession:
    def __init__(self, world: FakeWorld, *_args: Any) -> None:
        self.world = world

    async def __aenter__(self) -> "FakeBrowserSession":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    @asynccontextmanager
    async def page(self):
        yield FakePage()

    async def navigate(self, page: FakePage, url: str) -> None:
        page.url = self.world.redirects.get(url, url)

    async def challenge_reason(self, page: FakePage) -> str | None:
        return "captcha" if page.url in self.world.challenges else None

    async def save_debug_artifacts(self, *_args: Any) -> dict[str, str]:
        return {}


class FakeAdapter:
    def __init__(self, site: str, search_url: str, world: FakeWorld) -> None:
        self.site_name = site
        self.search_url = search_url
        self.world = world

    async def scan_results(self, _page: FakePage) -> list[Listing]:
        self.world.search_calls.append(self.site_name)
        self.world.trace.append(f"search:{self.site_name}")
        value = self.world.search.get(self.site_name, [])
        if isinstance(value, Exception):
            raise value
        return list(value)

    async def fetch_details(self, page: FakePage, candidate: Listing) -> Listing:
        assert page.url == candidate.canonical_url
        identifier = candidate.listing_id or candidate.identity_key
        self.world.detail_calls.append((self.site_name, identifier))
        self.world.trace.append(f"detail:{self.site_name}:{identifier}")
        value = self.world.details.get((self.site_name, identifier), candidate)
        if isinstance(value, Exception):
            raise value
        return value

    async def has_explicit_empty_result_marker(self, _page: FakePage) -> bool:
        return self.site_name in self.world.explicit_empty

    def canonicalize_url(self, url: str) -> str:
        return url

    def extract_id(self, url: str) -> str | None:
        return url.rstrip("/").rsplit("/", 1)[-1] if url else None

    def diagnostic(self) -> dict[str, Any]:
        return {"site": self.site_name}


def dependencies(world: FakeWorld, sites: tuple[str, ...] = SITES):
    registry = {
        site: (
            lambda search_url, site=site: FakeAdapter(site, search_url, world)
        )
        for site in sites
    }

    def browser_factory(*args: Any) -> FakeBrowserSession:
        return FakeBrowserSession(world, *args)

    return registry, browser_factory


def run(
    app_config: AppConfig,
    world: FakeWorld,
    *,
    database: Database | None = None,
    dry_run: bool = False,
    run_id: str,
) -> dict[str, Any]:
    registry, browser_factory = dependencies(world, tuple(app_config.sites))
    return asyncio.run(
        run_watcher(
            app_config,
            database=database,
            dry_run=dry_run,
            run_id=run_id,
            now=datetime(2026, 8, 20, 8, 0),
            adapter_registry=registry,
            browser_session_factory=browser_factory,
        )
    )


def test_baseline_identical_then_new_opens_only_the_new_detail(tmp_path) -> None:
    app_config = config(tmp_path, "leboncoin", "seloger", "seventee")
    world = FakeWorld(
        search={site: [listing(site, "A")] for site in SITES},
    )
    with Database(app_config.database_path) as database:
        baseline = run(app_config, world, database=database, run_id="baseline")
        assert baseline["status"] == "BASELINE_CREATED"
        assert baseline["new"] == []
        assert world.detail_calls == []

        unchanged = run(app_config, world, database=database, run_id="unchanged")
        assert unchanged["status"] == "NO_CHANGE"
        assert unchanged["actionable_count"] == 0
        assert world.detail_calls == []

        new_summary = listing("seloger", "B", price=None, surface=None)
        new_detail = listing("seloger", "B", price=730, surface=34)
        world.search["seloger"] = [listing("seloger", "A"), new_summary]
        world.details[("seloger", "B")] = new_detail
        changed = run(app_config, world, database=database, run_id="new")

        assert changed["status"] == "CHANGES"
        assert [event["id"] for event in changed["new"]] == ["B"]
        assert changed["new"][0]["price_eur"] == 730
        assert world.detail_calls == [("seloger", "B")]
        first_detail = world.trace.index("detail:seloger:B")
        assert all(
            world.trace.index(f"search:{site}", 6) < first_detail for site in SITES
        )


def test_failed_site_never_increments_missing_and_can_return_normally(tmp_path) -> None:
    app_config = config(tmp_path, "seloger")
    world = FakeWorld(
        search={"seloger": [listing("seloger", "A"), listing("seloger", "B")]}
    )
    with Database(app_config.database_path) as database:
        run(app_config, world, database=database, run_id="baseline")

        world.search["seloger"] = TimeoutError("offline timeout")
        failed = run(app_config, world, database=database, run_id="failure")
        assert failed["status"] == "ERROR"
        assert failed["complete"] is False
        assert database.get_listing("seloger", "A").missing_count == 0
        assert database.get_listing("seloger", "B").missing_count == 0

        world.search["seloger"] = [listing("seloger", "A"), listing("seloger", "B")]
        recovered = run(app_config, world, database=database, run_id="recovered")
        assert recovered["status"] == "NO_CHANGE"
        assert recovered["complete"] is True
        assert database.get_listing("seloger", "B").missing_count == 0


def test_empty_without_explicit_marker_is_suspicious_and_mutates_nothing(
    tmp_path,
) -> None:
    app_config = config(tmp_path, "seloger")
    world = FakeWorld(search={"seloger": [listing("seloger", "A")]})
    with Database(app_config.database_path) as database:
        run(app_config, world, database=database, run_id="baseline")
        before = database.get_site_state("seloger")

        world.search["seloger"] = []
        result = run(app_config, world, database=database, run_id="empty")

        assert result["status"] == "ERROR"
        assert result["sites"]["seloger"]["status"] == "SUSPICIOUS_RESULT"
        assert result["missing_from_search"] == []
        assert database.get_site_state("seloger") == before
        assert database.get_listing("seloger", "A").missing_count == 0


def test_empty_first_scan_is_not_accepted_as_a_baseline(tmp_path) -> None:
    app_config = config(tmp_path, "seventee")
    world = FakeWorld(search={"seventee": []})
    with Database(app_config.database_path) as database:
        result = run(app_config, world, database=database, run_id="empty-first")
        assert result["sites"]["seventee"]["status"] == "SUSPICIOUS_RESULT"
        assert database.has_site_baseline("seventee") is False


def test_dry_run_with_absent_database_never_creates_it(tmp_path) -> None:
    database_path = tmp_path / "absent" / "state.db"
    app_config = config(
        tmp_path,
        "leboncoin",
        database_path=database_path,
    )
    world = FakeWorld(search={"leboncoin": [listing("leboncoin", "A")]})
    registry, browser_factory = dependencies(world, ("leboncoin",))

    result = asyncio.run(
        run_watcher(
            app_config,
            dry_run=True,
            run_id="preview",
            now=datetime(2026, 8, 20, 8, 0),
            adapter_registry=registry,
            browser_session_factory=browser_factory,
        )
    )

    assert result["status"] == "BASELINE_CREATED"
    assert result["dry_run"] is True
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_baseline_is_independent_after_one_site_recovers(tmp_path) -> None:
    app_config = config(tmp_path, "leboncoin", "seloger")
    world = FakeWorld(
        search={
            "leboncoin": [listing("leboncoin", "A")],
            "seloger": RuntimeError("parser failed"),
        }
    )
    with Database(app_config.database_path) as database:
        first = run(app_config, world, database=database, run_id="partial")
        assert first["status"] == "PARTIAL_FAILURE"
        assert database.has_site_baseline("leboncoin") is True
        assert database.has_site_baseline("seloger") is False

        world.search["seloger"] = [listing("seloger", "S")]
        second = run(app_config, world, database=database, run_id="recovery")
        assert second["status"] == "BASELINE_CREATED"
        assert second["new"] == []
        assert database.has_site_baseline("seloger") is True
        assert world.detail_calls == []


def test_detail_failure_keeps_summary_persists_it_and_continues(tmp_path) -> None:
    app_config = config(tmp_path, "seloger")
    world = FakeWorld(search={"seloger": [listing("seloger", "A")]})
    with Database(app_config.database_path) as database:
        run(app_config, world, database=database, run_id="baseline")

        sparse_b = listing("seloger", "B", price=None, surface=None)
        sparse_c = listing("seloger", "C", price=None, surface=None)
        world.search["seloger"] = [listing("seloger", "A"), sparse_b, sparse_c]
        world.details[("seloger", "B")] = TimeoutError("detail timeout")
        world.details[("seloger", "C")] = listing(
            "seloger", "C", price=780, surface=40
        )

        result = run(app_config, world, database=database, run_id="details")

        assert result["status"] == "CHANGES"
        assert result["complete"] is False
        assert len(result["sites"]["seloger"]["detail_errors"]) == 1
        assert result["sites"]["seloger"]["detail_attempted"] == 2
        assert result["sites"]["seloger"]["detail_succeeded"] == 1
        assert [event["id"] for event in result["new"]] == ["B", "C"]
        assert result["new"][0]["price_eur"] is None
        assert result["new"][1]["price_eur"] == 780
        assert world.detail_calls == [("seloger", "B"), ("seloger", "C")]
        assert database.get_listing("seloger", "B").listing.price_eur is None
        assert database.get_listing("seloger", "C").listing.price_eur == 780


def test_material_update_opens_only_that_detail_and_uses_detail_value(tmp_path) -> None:
    app_config = config(tmp_path, "seloger")
    world = FakeWorld(search={"seloger": [listing("seloger", "A", price=750)]})
    with Database(app_config.database_path) as database:
        run(app_config, world, database=database, run_id="baseline")

        world.search["seloger"] = [listing("seloger", "A", price=720)]
        world.details[("seloger", "A")] = listing("seloger", "A", price=710)
        changed = run(app_config, world, database=database, run_id="price")

        assert changed["status"] == "CHANGES"
        assert world.detail_calls == [("seloger", "A")]
        assert changed["updated"][0]["changes"]["price_eur"] == {
            "before": 750,
            "after": 710,
        }

        world.detail_calls.clear()
        world.search["seloger"] = [listing("seloger", "A", price=710)]
        unchanged = run(app_config, world, database=database, run_id="same-price")
        assert unchanged["status"] == "NO_CHANGE"
        assert world.detail_calls == []


def test_detail_challenge_marks_site_untrusted_and_persists_nothing(tmp_path) -> None:
    app_config = config(tmp_path, "seloger")
    world = FakeWorld(search={"seloger": [listing("seloger", "A")]})
    with Database(app_config.database_path) as database:
        run(app_config, world, database=database, run_id="baseline")

        new_listing = listing("seloger", "B")
        world.search["seloger"] = [listing("seloger", "A"), new_listing]
        world.challenges.add(new_listing.canonical_url)
        challenged = run(app_config, world, database=database, run_id="challenge")

        assert challenged["status"] == "ERROR"
        assert challenged["sites"]["seloger"]["status"] == "CHALLENGE"
        assert challenged["new"] == []
        assert database.get_listing("seloger", "B") is None


def test_detail_redirect_is_not_parsed_as_the_candidate(tmp_path) -> None:
    app_config = config(tmp_path, "seloger")
    existing = listing("seloger", "A")
    world = FakeWorld(search={"seloger": [existing]})
    with Database(app_config.database_path) as database:
        run(app_config, world, database=database, run_id="baseline")

        candidate = listing("seloger", "B", price=None, surface=None)
        world.search["seloger"] = [existing, candidate]
        world.redirects[candidate.canonical_url] = existing.canonical_url
        result = run(app_config, world, database=database, run_id="redirect")

        assert result["status"] == "CHANGES"
        assert result["sites"]["seloger"]["detail_failures"] == 1
        assert world.detail_calls == []
        assert result["new"][0]["price_eur"] is None
