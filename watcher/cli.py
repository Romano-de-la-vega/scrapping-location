"""Command-line boundary for the deterministic property watcher.

Only this module writes the command result to stdout.  The runner and the
diagnostic helpers return ordinary dictionaries so every invocation emits one
compact JSON document, including configuration and fatal errors.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any, Sequence

from watcher.browser import BrowserSession
from watcher.config import AppConfig, ConfigError, load_config
from watcher.database import Database
from watcher.locks import AlreadyRunningError, GlobalLock
from watcher.output import configure_logging, write_json
from watcher.runner import run_watcher
from watcher.sites import create_adapter


SITE_NAMES: tuple[str, ...] = ("leboncoin", "seloger", "seventee")


class CliUsageError(ValueError):
    """Raised instead of letting argparse terminate without a JSON result."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="watcher")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="scan configured sites")
    run_parser.add_argument("--config", type=Path, default=Path("config.json"))
    run_parser.add_argument("--site", choices=SITE_NAMES)
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--verbose", action="store_true")

    status_parser = commands.add_parser("status", help="show stored state")
    status_parser.add_argument("--config", type=Path, default=Path("config.json"))

    history_parser = commands.add_parser("history", help="show recent events")
    history_parser.add_argument("--config", type=Path, default=Path("config.json"))
    history_parser.add_argument("--limit", type=_positive_int, default=20)

    diagnose_parser = commands.add_parser(
        "diagnose", help="inspect one search page without changing state"
    )
    diagnose_parser.add_argument(
        "--config", type=Path, default=Path("config.json")
    )
    diagnose_parser.add_argument("--site", choices=SITE_NAMES, required=True)
    return parser


def _message(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    return (message or type(exc).__name__)[:500]


def _error_payload(code: str, exc: BaseException) -> dict[str, Any]:
    return {
        "status": "ERROR",
        "error": {
            "code": code,
            "message": _message(exc),
        },
    }


def _configured_site(config: AppConfig, site: str) -> Any:
    try:
        return config.sites[site]
    except KeyError as exc:
        raise ConfigError(f"site is not configured: {site}") from exc


def _run_command(
    args: argparse.Namespace,
    config: AppConfig,
    logger: logging.Logger,
) -> dict[str, Any]:
    if args.site is not None:
        _configured_site(config, args.site)
    with GlobalLock(config.lock_path):
        return asyncio.run(
            run_watcher(
                config,
                site_names=args.site,
                dry_run=args.dry_run,
                logger=logger,
            )
        )


def _status_command(config: AppConfig) -> dict[str, Any]:
    # Database(dry_run=True) uses a read-only connection for an existing file
    # and an in-memory schema when the configured file does not exist.
    with Database(config.database_path, dry_run=True) as database:
        status = database.get_status()
    return {"status": "OK", **status}


def _history_command(args: argparse.Namespace, config: AppConfig) -> dict[str, Any]:
    with Database(config.database_path, dry_run=True) as database:
        events = database.get_recent_events(args.limit)
    return {"status": "OK", "limit": args.limit, "events": events}


async def _safe_page_title(page: Any) -> str | None:
    try:
        value = await page.title()
    except Exception:
        return None
    title = str(value).strip()
    return title or None


def _loaded_url(page: Any) -> str | None:
    value = getattr(page, "url", None)
    if value is None:
        return None
    url = str(value).strip()
    return url or None


def _diagnostic_payload(
    *,
    site: str,
    loaded_url: str | None,
    page_title: str | None,
    challenge_reason: str | None,
    diagnostics: dict[str, Any] | None = None,
    listing_count: int = 0,
) -> dict[str, Any]:
    values = diagnostics or {}
    challenge = challenge_reason is not None
    return {
        "status": "CHALLENGE" if challenge else "OK",
        "site": site,
        "loaded_url": values.get("loaded_url") or loaded_url,
        "page_title": values.get("page_title") or page_title,
        "candidate_links": int(values.get("candidate_links", 0)),
        "valid_ids": int(values.get("valid_ids", 0)),
        "listings": int(values.get("listings", listing_count)),
        "duplicates": int(values.get("duplicates", 0)),
        "rejected_candidates": int(values.get("rejected_candidates", 0)),
        "challenge": challenge,
        "challenge_reason": challenge_reason,
    }


async def _diagnose(config: AppConfig, site: str) -> dict[str, Any]:
    site_config = _configured_site(config, site)
    adapter = create_adapter(site, site_config.search_url)
    session = BrowserSession(config.browser, config.scan)
    async with session as browser:
        async with browser.page() as page:
            await browser.navigate(page, adapter.search_url)
            loaded_url = _loaded_url(page)
            page_title = await _safe_page_title(page)
            challenge_reason = await browser.challenge_reason(page)
            if challenge_reason is not None:
                return _diagnostic_payload(
                    site=site,
                    loaded_url=loaded_url,
                    page_title=page_title,
                    challenge_reason=challenge_reason,
                )

            listings = await adapter.scan_results(page)
            # A challenge can materialize while client-side results are loading.
            challenge_reason = await browser.challenge_reason(page)
            diagnostics = dict(adapter.diagnostic())
            return _diagnostic_payload(
                site=site,
                loaded_url=_loaded_url(page) or loaded_url,
                page_title=await _safe_page_title(page) or page_title,
                challenge_reason=challenge_reason,
                diagnostics=diagnostics,
                listing_count=len(listings),
            )


def _diagnose_command(args: argparse.Namespace, config: AppConfig) -> dict[str, Any]:
    with GlobalLock(config.lock_path):
        return asyncio.run(_diagnose(config, args.site))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except CliUsageError as exc:
        write_json(_error_payload("USAGE_ERROR", exc))
        return 2

    logger: logging.Logger | None = None
    try:
        config = load_config(args.config)
        logger = configure_logging(
            config.log_path,
            verbose=bool(getattr(args, "verbose", False)),
        )
        if args.command == "run":
            payload = _run_command(args, config, logger)
        elif args.command == "status":
            payload = _status_command(config)
        elif args.command == "history":
            payload = _history_command(args, config)
        else:
            payload = _diagnose_command(args, config)
        exit_code = 0
    except AlreadyRunningError:
        payload = {"status": "ALREADY_RUNNING"}
        exit_code = 0
    except ConfigError as exc:
        payload = _error_payload("CONFIG_ERROR", exc)
        exit_code = 2
    except (OSError, UnicodeError) as exc:
        # File decoding/access failures while loading configuration or state are
        # operationally fatal but still part of the stable JSON protocol.
        if logger is not None:
            logger.exception("CLI I/O failure")
        payload = _error_payload("IO_ERROR", exc)
        exit_code = 1
    except Exception as exc:
        if logger is not None:
            logger.exception("CLI fatal error")
        payload = _error_payload("FATAL_ERROR", exc)
        exit_code = 1

    write_json(payload)
    return exit_code


__all__ = ["build_parser", "main"]
