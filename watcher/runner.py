"""Async two-pass orchestration for deterministic watcher runs.

The runner deliberately does not acquire the process lock and does not print.
Those are CLI responsibilities.  Its dependencies are injectable so the full
search/diff/detail/persistence flow can be exercised without a browser or the
network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from inspect import isawaitable
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from watcher.browser import BrowserSession
from watcher.config import AppConfig
from watcher.database import Database, open_database
from watcher.diff import compare_site, deduplicate_listings, failed_site_diff
from watcher.models import EventType, Listing, SiteDiff, SiteStatus
from watcher.sites import AdapterFactory, SITE_ADAPTERS, create_adapter


BrowserSessionFactory = Callable[[Any, Any], Any]
DatabaseFactory = Callable[..., Database]

_DETAIL_EVENT_TYPES = frozenset(
    {EventType.NEW, EventType.UPDATED, EventType.BECAME_ELIGIBLE}
)
_LISTING_FIELDS: tuple[str, ...] = (
    "listing_id",
    "canonical_url",
    "title",
    "price_eur",
    "surface_m2",
    "rooms",
    "location",
    "postal_code",
)


@dataclass(slots=True)
class _SiteWork:
    site: str
    adapter: Any | None = None
    search_listings: tuple[Listing, ...] = ()
    pre_diff: SiteDiff | None = None
    final_diff: SiteDiff | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    detail_attempted: int = 0
    detail_succeeded: int = 0
    detail_failures: list[str] = field(default_factory=list)
    duration_ms: int = 0


def _selected_site_names(
    config: AppConfig,
    site_names: Iterable[str] | str | None,
) -> tuple[str, ...]:
    if site_names is None:
        names = [
            name
            for name, site_config in config.sites.items()
            if site_config.enabled
        ]
    else:
        raw_names = (site_names,) if isinstance(site_names, str) else site_names
        names = [str(name).strip().lower() for name in raw_names]

    selected = tuple(sorted(dict.fromkeys(name for name in names if name)))
    if not selected:
        raise ValueError("no enabled site is configured")
    missing = [name for name in selected if name not in config.sites]
    if missing:
        raise ValueError(f"site is not configured: {missing[0]}")
    return selected


def _started_at(config: AppConfig, value: datetime | Callable[[], datetime] | None) -> datetime:
    current = value() if callable(value) else value
    timezone = ZoneInfo(config.timezone)
    if current is None:
        return datetime.now(timezone)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone)
    return current.astimezone(timezone)


def _error_reason(exc: BaseException) -> str:
    message = str(exc).strip()
    reason = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return " ".join(reason.split())[:500]


def _site_previous_count(database: Database, site: str) -> int:
    metadata = database.get_site_state(site)
    return int(metadata["last_result_count"]) if metadata is not None else 0


def _failed_diff(
    database: Database,
    site: str,
    *,
    status: SiteStatus,
    reason: str,
) -> SiteDiff:
    return failed_site_diff(
        site,
        status=status,
        previous_count=_site_previous_count(database, site),
        reason=reason,
    )


def _suspicious_empty_diff(database: Database, site: str) -> SiteDiff:
    return SiteDiff(
        site=site,
        status=SiteStatus.SUSPICIOUS_RESULT,
        baseline_created=False,
        suspicious=True,
        previous_count=_site_previous_count(database, site),
        current_count=0,
        reason="zero listings without a validated explicit empty-result marker",
    )


async def _has_explicit_empty_marker(adapter: Any, page: Any) -> bool:
    """Ask an adapter for positive empty-state evidence, when it has any.

    None of the production adapters currently exposes such a validated marker.
    The narrow hook exists so a future adapter (and offline integrations) can
    explicitly opt in after implementing a DOM-backed check.
    """

    marker = getattr(adapter, "has_explicit_empty_result_marker", None)
    if marker is None:
        return False
    try:
        result = marker(page) if callable(marker) else marker
        if isawaitable(result):
            result = await result
        return result is True
    except Exception:
        return False


def _pure_site_diff(
    database: Database,
    config: AppConfig,
    site: str,
    listings: Sequence[Listing],
    observed_at: str,
) -> SiteDiff:
    metadata = database.get_site_state(site)
    return compare_site(
        database.load_listing_states(site),
        listings,
        baseline_exists=metadata is not None,
        site=site,
        previous_count=(
            int(metadata["last_result_count"]) if metadata is not None else 0
        ),
        criteria=config.criteria,
        missing_threshold=config.diff.missing_threshold,
        suspicious_result_ratio=config.diff.suspicious_result_ratio,
        suspicious_min_previous_count=config.diff.suspicious_min_previous_count,
        observed_at=observed_at,
    )


def _listing_sort_key(listing: Listing) -> tuple[str, str]:
    return (listing.listing_id or "", listing.canonical_url or "")


def _event_sort_key(event: Any) -> tuple[str, str, str, str]:
    return (
        event.site,
        event.listing_id or "",
        event.canonical_url or "",
        event.event_type.value,
    )


def _same_listing(first: Listing, second: Listing) -> bool:
    if first.site != second.site:
        return False
    if first.listing_id is not None and second.listing_id is not None:
        return first.listing_id == second.listing_id
    return (
        first.canonical_url is not None
        and first.canonical_url == second.canonical_url
    )


def _merge_detail(search_and_old: Listing, detail: Listing) -> Listing:
    """Merge a detail observation with precedence ``detail > search > old``.

    ``search_and_old`` is the pre-diff observation: the pure diff engine has
    already filled search omissions from stored state.  Only explicit non-null
    detail values replace it.  Eligibility is intentionally cleared so the
    final pure diff recomputes it from the merged evidence.
    """

    if detail.site != search_and_old.site:
        raise ValueError("detail observation belongs to another site")
    if not _same_listing(search_and_old, detail):
        raise ValueError("detail observation changed listing identity")
    values = {
        name: (
            getattr(detail, name)
            if getattr(detail, name) is not None
            else getattr(search_and_old, name)
        )
        for name in _LISTING_FIELDS
    }
    return Listing(site=search_and_old.site, eligibility=None, **values)


def _validate_loaded_detail_page(
    adapter: Any,
    candidate: Listing,
    page: Any,
) -> None:
    """Reject redirects to another page before parsing its metadata."""

    canonicalize = getattr(adapter, "canonicalize_url", None)
    extract_id = getattr(adapter, "extract_id", None)
    if not callable(canonicalize) or not callable(extract_id):
        return
    expected = canonicalize(candidate.canonical_url or "")
    loaded = canonicalize(str(getattr(page, "url", "") or ""))
    if not expected or not loaded:
        raise RuntimeError("detail navigation left the expected site")
    expected_id = extract_id(expected)
    loaded_id = extract_id(loaded)
    if expected_id is not None:
        if loaded_id != expected_id:
            raise RuntimeError("detail navigation reached another listing")
    elif loaded != expected:
        raise RuntimeError("detail navigation reached an unexpected URL")


def _mark_detail_challenge(
    database: Database,
    work: _SiteWork,
    candidate: Listing,
    reason: str,
) -> None:
    work.detail_failures.append(
        f"{candidate.identity_key}: detail challenge: {reason}"
    )
    work.final_diff = _failed_diff(
        database,
        work.site,
        status=SiteStatus.CHALLENGE,
        reason=f"detail challenge: {reason}",
    )


async def _scan_search_pages(
    browser: Any,
    database: Database,
    works: Mapping[str, _SiteWork],
    config: AppConfig,
    logger: logging.Logger,
) -> None:
    """Complete every search-page scan before any comparison or detail page."""

    for site, work in works.items():
        if work.adapter is None:
            continue
        site_started = perf_counter()
        try:
            async with browser.page() as page:
                await browser.navigate(page, work.adapter.search_url)
                challenge = await browser.challenge_reason(page)
                if challenge:
                    work.final_diff = _failed_diff(
                        database,
                        site,
                        status=SiteStatus.CHALLENGE,
                        reason=challenge,
                    )
                    continue

                try:
                    extracted = await work.adapter.scan_results(page)
                except Exception:
                    if config.debug_artifacts_on_error:
                        try:
                            await browser.save_debug_artifacts(
                                page, config.debug_directory, site
                            )
                        except Exception as artifact_error:
                            logger.warning(
                                "%s debug artifacts failed: %s",
                                site,
                                _error_reason(artifact_error),
                            )
                    raise
                challenge = await browser.challenge_reason(page)
                if challenge:
                    work.final_diff = _failed_diff(
                        database,
                        site,
                        status=SiteStatus.CHALLENGE,
                        reason=challenge,
                    )
                    continue

                listings = tuple(
                    sorted(deduplicate_listings(extracted), key=_listing_sort_key)
                )
                if not listings and not await _has_explicit_empty_marker(
                    work.adapter, page
                ):
                    work.final_diff = _suspicious_empty_diff(database, site)
                    if config.debug_artifacts_on_error:
                        try:
                            await browser.save_debug_artifacts(
                                page, config.debug_directory, site
                            )
                        except Exception as artifact_error:
                            logger.warning(
                                "%s debug artifacts failed: %s",
                                site,
                                _error_reason(artifact_error),
                            )
                    continue
                work.search_listings = listings
                work.diagnostics = work.adapter.diagnostic()
        except Exception as exc:
            logger.exception("%s search scan failed", site)
            work.final_diff = _failed_diff(
                database,
                site,
                status=SiteStatus.ERROR,
                reason=_error_reason(exc),
            )
        finally:
            work.duration_ms += max(
                0, int((perf_counter() - site_started) * 1_000)
            )


def _build_pre_diffs(
    database: Database,
    config: AppConfig,
    works: Mapping[str, _SiteWork],
    observed_at: str,
) -> None:
    for site, work in works.items():
        if work.final_diff is not None:
            continue
        try:
            work.pre_diff = _pure_site_diff(
                database, config, site, work.search_listings, observed_at
            )
            if work.pre_diff.suspicious:
                work.final_diff = work.pre_diff
        except Exception as exc:
            work.final_diff = _failed_diff(
                database,
                site,
                status=SiteStatus.ERROR,
                reason=_error_reason(exc),
            )


async def _enrich_relevant_listings(
    browser: Any,
    database: Database,
    works: Mapping[str, _SiteWork],
    config: AppConfig,
    logger: logging.Logger,
) -> None:
    """Open details only for preliminary NEW/UPDATED/BECAME events."""

    for work in works.values():
        if work.final_diff is not None or work.pre_diff is None:
            continue
        current = list(work.pre_diff.current_listings)
        candidates = {
            event.after.identity_key: event.after
            for event in work.pre_diff.events
            if event.event_type in _DETAIL_EVENT_TYPES and event.after is not None
        }
        detail_started = perf_counter()
        for candidate in sorted(candidates.values(), key=_listing_sort_key):
            work.detail_attempted += 1
            if candidate.canonical_url is None:
                work.detail_failures.append(
                    f"{candidate.identity_key}: no canonical detail URL"
                )
                continue
            try:
                async with browser.page() as page:
                    await browser.navigate(page, candidate.canonical_url)
                    challenge = await browser.challenge_reason(page)
                    if challenge:
                        _mark_detail_challenge(
                            database, work, candidate, challenge
                        )
                        break
                    _validate_loaded_detail_page(work.adapter, candidate, page)
                    try:
                        detail = await work.adapter.fetch_details(page, candidate)
                    except Exception:
                        challenge = await browser.challenge_reason(page)
                        if challenge:
                            _mark_detail_challenge(
                                database, work, candidate, challenge
                            )
                            break
                        if config.debug_artifacts_on_error:
                            try:
                                await browser.save_debug_artifacts(
                                    page, config.debug_directory, work.site
                                )
                            except Exception as artifact_error:
                                logger.warning(
                                    "%s detail debug artifacts failed: %s",
                                    work.site,
                                    _error_reason(artifact_error),
                                )
                        raise
                    challenge = await browser.challenge_reason(page)
                    if challenge:
                        _mark_detail_challenge(
                            database, work, candidate, challenge
                        )
                        break
                    if not isinstance(detail, Listing):
                        raise TypeError("fetch_details must return Listing")
                    merged = _merge_detail(candidate, detail)
            except Exception as exc:
                logger.exception(
                    "%s detail extraction failed for %s",
                    work.site,
                    candidate.identity_key,
                )
                work.detail_failures.append(
                    f"{candidate.identity_key}: {_error_reason(exc)}"
                )
                continue

            for index, listing in enumerate(current):
                if _same_listing(listing, candidate):
                    current[index] = merged
                    work.detail_succeeded += 1
                    break
        work.duration_ms += max(
            0, int((perf_counter() - detail_started) * 1_000)
        )
        work.search_listings = tuple(sorted(current, key=_listing_sort_key))


def _build_final_diffs(
    database: Database,
    config: AppConfig,
    works: Mapping[str, _SiteWork],
    observed_at: str,
) -> None:
    for site, work in works.items():
        if work.final_diff is not None:
            continue
        try:
            work.final_diff = _pure_site_diff(
                database, config, site, work.search_listings, observed_at
            )
        except Exception as exc:
            work.final_diff = _failed_diff(
                database,
                site,
                status=SiteStatus.ERROR,
                reason=_error_reason(exc),
            )


def _is_reliable(result: SiteDiff) -> bool:
    return result.status in {SiteStatus.OK, SiteStatus.BASELINE_CREATED}


def _overall_status(
    works: Mapping[str, _SiteWork],
    events: Sequence[Any],
) -> tuple[str, bool]:
    if not works:
        return "ERROR", False
    reliable_count = sum(
        work.final_diff is not None and _is_reliable(work.final_diff)
        for work in works.values()
    )
    detail_failed = any(work.detail_failures for work in works.values())
    complete = reliable_count == len(works) and not detail_failed
    if events:
        return "CHANGES", complete
    if reliable_count == 0:
        return "ERROR", False
    if not complete:
        return "PARTIAL_FAILURE", False
    if any(
        work.final_diff is not None and work.final_diff.baseline_created
        for work in works.values()
    ):
        return "BASELINE_CREATED", True
    return "NO_CHANGE", True


def _site_payload(work: _SiteWork) -> dict[str, Any]:
    result = work.final_diff
    if result is None:  # Defensive; all normal paths assign a final result.
        return {"status": SiteStatus.ERROR.value, "count": 0}
    payload: dict[str, Any] = {
        "status": result.status.value,
        "count": result.current_count,
        "duration_ms": work.duration_ms,
    }
    if result.status is SiteStatus.SUSPICIOUS_RESULT:
        payload["previous_count"] = result.previous_count
    if result.reason:
        payload["reason"] = result.reason
    if work.diagnostics:
        payload["diagnostics"] = work.diagnostics
    if work.detail_attempted:
        payload["detail_attempted"] = work.detail_attempted
        payload["detail_succeeded"] = work.detail_succeeded
    if work.detail_failures:
        payload["detail_failures"] = len(work.detail_failures)
        payload["detail_errors"] = work.detail_failures
    return payload


def _result_payload(
    works: Mapping[str, _SiteWork],
    *,
    run_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    events = sorted(
        (
            event
            for work in works.values()
            if work.final_diff is not None and _is_reliable(work.final_diff)
            for event in work.final_diff.events
        ),
        key=_event_sort_key,
    )
    status, complete = _overall_status(works, events)

    def serialized(event_type: EventType) -> list[dict[str, Any]]:
        return [
            event.to_dict()
            for event in events
            if event.event_type is event_type
        ]

    return {
        "status": status,
        "run_id": run_id,
        "dry_run": bool(dry_run),
        "complete": complete,
        "sites": {
            site: _site_payload(work) for site, work in works.items()
        },
        "new": serialized(EventType.NEW),
        "updated": serialized(EventType.UPDATED),
        "became_eligible": serialized(EventType.BECAME_ELIGIBLE),
        "missing_from_search": serialized(EventType.MISSING_FROM_SEARCH),
        "actionable_count": sum(event.actionable for event in events),
    }


def _persist_run(
    database: Database,
    config: AppConfig,
    works: Mapping[str, _SiteWork],
    payload: Mapping[str, Any],
    *,
    run_id: str,
    started_at: str,
    duration_ms: int,
) -> None:
    """Persist every reliable site and its events as one atomic unit."""

    events = [
        event
        for work in works.values()
        if work.final_diff is not None and _is_reliable(work.final_diff)
        for event in work.final_diff.events
    ]
    with database.transaction():
        database.start_run(run_id, started_at=started_at)
        for site, work in works.items():
            if work.final_diff is None or not _is_reliable(work.final_diff):
                continue
            database.process_site_scan(
                site,
                work.search_listings,
                run_id,
                criteria=config.criteria,
                diff_config=config.diff,
                observed_at=started_at,
            )
        database.finish_run(
            run_id,
            status=str(payload["status"]),
            site_counts={
                site: (
                    work.final_diff.current_count
                    if work.final_diff is not None and _is_reliable(work.final_diff)
                    else None
                )
                for site, work in works.items()
            },
            new_count=sum(event.event_type is EventType.NEW for event in events),
            updated_count=sum(
                event.event_type in {EventType.UPDATED, EventType.BECAME_ELIGIBLE}
                for event in events
            ),
            duration_ms=duration_ms,
            started_at=started_at,
        )


async def run_watcher(
    config: AppConfig,
    *,
    site_names: Iterable[str] | str | None = None,
    dry_run: bool = False,
    browser_session_factory: BrowserSessionFactory = BrowserSession,
    adapter_registry: Mapping[str, AdapterFactory] = SITE_ADAPTERS,
    database_factory: DatabaseFactory = open_database,
    database: Database | None = None,
    database_path: str | Path | None = None,
    now: datetime | Callable[[], datetime] | None = None,
    run_id: str | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Run search scans, pure diffs, conditional details, and atomic storage.

    A supplied ``database`` remains owned by the caller.  Otherwise the runner
    opens ``database_path`` (or ``config.database_path``) in the appropriate
    normal/read-only/memory mode and closes it before returning.
    """

    if database is not None and database_path is not None:
        raise ValueError("pass either database or database_path, not both")

    started_clock = perf_counter()
    log = logger or logging.getLogger("watcher")
    started = _started_at(config, now)
    observed_at = started.isoformat()
    effective_run_id = str(run_id) if run_id is not None else observed_at
    selected = _selected_site_names(config, site_names)

    owns_database = database is None
    db = database or database_factory(
        database_path or config.database_path,
        dry_run=dry_run,
    )
    try:
        works: dict[str, _SiteWork] = {
            site: _SiteWork(site=site) for site in selected
        }
        for site, work in works.items():
            try:
                work.adapter = create_adapter(
                    site,
                    config.sites[site].search_url,
                    registry=adapter_registry,
                )
            except Exception as exc:
                work.final_diff = _failed_diff(
                    db,
                    site,
                    status=SiteStatus.ERROR,
                    reason=_error_reason(exc),
                )

        try:
            session = browser_session_factory(config.browser, config.scan)
            async with session as browser:
                await _scan_search_pages(browser, db, works, config, log)
                # This entire pass is read-only and starts only after every
                # search page has either succeeded or received a terminal state.
                _build_pre_diffs(db, config, works, observed_at)
                await _enrich_relevant_listings(browser, db, works, config, log)
        except Exception as exc:
            log.exception("browser session failed")
            reason = _error_reason(exc)
            for site, work in works.items():
                if work.final_diff is None and work.pre_diff is None:
                    work.final_diff = _failed_diff(
                        db,
                        site,
                        status=SiteStatus.ERROR,
                        reason=reason,
                    )
                elif work.final_diff is None:
                    work.detail_failures.append(f"browser session: {reason}")

        _build_final_diffs(db, config, works, observed_at)
        payload = _result_payload(
            works,
            run_id=effective_run_id,
            dry_run=dry_run,
        )
        duration_ms = max(0, int((perf_counter() - started_clock) * 1_000))
        for site, work in works.items():
            result = work.final_diff
            site_events = result.events if result is not None else ()
            log.info(
                "site=%s status=%s duration_ms=%d count=%d new=%d updated=%d errors=%d",
                site,
                result.status.value if result is not None else "ERROR",
                work.duration_ms,
                result.current_count if result is not None else 0,
                sum(event.event_type is EventType.NEW for event in site_events),
                sum(
                    event.event_type
                    in {EventType.UPDATED, EventType.BECAME_ELIGIBLE}
                    for event in site_events
                ),
                len(work.detail_failures)
                + int(
                    result is None
                    or result.status
                    in {SiteStatus.ERROR, SiteStatus.CHALLENGE}
                ),
            )
        if not dry_run:
            _persist_run(
                db,
                config,
                works,
                payload,
                run_id=effective_run_id,
                started_at=observed_at,
                duration_ms=duration_ms,
            )
        log.info(
            "run_id=%s status=%s duration_ms=%d actionable=%d",
            effective_run_id,
            payload["status"],
            duration_ms,
            payload["actionable_count"],
        )
        return payload
    finally:
        if owns_database:
            close = getattr(db, "close", None)
            if callable(close):
                close()


# The concise alias is useful to callers that already import from
# ``watcher.runner``; the CLI uses the more explicit public name above.
run = run_watcher


__all__ = ["run", "run_watcher"]
