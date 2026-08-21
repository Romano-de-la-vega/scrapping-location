"""Central dispatcher for many independent property follows.

The dispatcher is intentionally deterministic. It scans only follows that are due,
routes each result to the follow's Telegram chat, and invokes Hermes one-shot only
when a follow explicitly enables ``run_llm_on_new`` and actionable events exist.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from watcher import follows


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = follows.DEFAULT_ROOT
DEFAULT_MAX_WORKERS = 4
DEFAULT_SCAN_TIMEOUT = 150
DEFAULT_LLM_TIMEOUT = 240
DEFAULT_RETRY_MINUTES = 2
LOCK_STALE_SECONDS = 15 * 60
TELEGRAM_MESSAGE_LIMIT = 3900
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
INTERVAL_RE = re.compile(
    r"^(?:every\s+)?(?P<value>\d+)\s*(?P<unit>m|min|mins|minute|minutes|h|hr|hrs|hour|hours)$",
    re.IGNORECASE,
)


class DispatcherError(RuntimeError):
    """Operational dispatcher failure."""


class DispatcherAlreadyRunning(DispatcherError):
    """Raised when a previous dispatcher tick is still active."""


def now_local() -> datetime:
    return datetime.now().astimezone()


def _read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DispatcherError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DispatcherError(f"JSON root must be an object: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.astimezone()
    return result


def parse_interval_minutes(schedule: str) -> int:
    """Parse the interval forms used by property-follow, e.g. ``every 5m``."""

    text = str(schedule or "").strip()
    match = INTERVAL_RE.fullmatch(text)
    if not match:
        raise DispatcherError(
            f"unsupported follow schedule {text!r}; use an interval such as 'every 5m' or 'every 1h'"
        )
    value = int(match.group("value"))
    if value < 1:
        raise DispatcherError("follow interval must be at least one minute")
    unit = match.group("unit").lower()
    return value * 60 if unit.startswith("h") else value


def follow_interval_minutes(manifest: dict[str, Any]) -> int:
    explicit = manifest.get("interval_minutes")
    if explicit is not None:
        try:
            value = int(explicit)
        except (TypeError, ValueError) as exc:
            raise DispatcherError("interval_minutes must be an integer") from exc
        if value < 1:
            raise DispatcherError("interval_minutes must be at least 1")
        return value
    return parse_interval_minutes(str(manifest.get("schedule") or "every 5m"))


def _state_path(follow_dir: Path) -> Path:
    return follow_dir / "dispatcher_state.json"


def _pending_path(follow_dir: Path) -> Path:
    return follow_dir / "pending_delivery.json"


def load_dispatch_state(follow_dir: Path) -> dict[str, Any]:
    return _read_json(
        _state_path(follow_dir),
        {
            "last_run_at": None,
            "next_run_at": None,
            "last_success_at": None,
            "last_error": None,
            "failure_streak": 0,
        },
    )


def save_dispatch_state(follow_dir: Path, state: dict[str, Any]) -> None:
    _write_json(_state_path(follow_dir), state)


def is_due(manifest: dict[str, Any], state: dict[str, Any], now: datetime) -> bool:
    if not bool(manifest.get("enabled", True)):
        return False
    next_run = _parse_dt(state.get("next_run_at"))
    return next_run is None or next_run <= now


def _next_run(manifest: dict[str, Any], from_time: datetime) -> datetime:
    return from_time + timedelta(minutes=follow_interval_minutes(manifest))


def list_follow_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path.parent for path in root.glob("*/follow.json"))


def load_manifest(follow_dir: Path) -> dict[str, Any]:
    return _read_json(follow_dir / "follow.json")


def _telegram_token() -> str:
    token = (
        os.getenv("PROPERTY_DISPATCHER_TELEGRAM_TOKEN")
        or os.getenv("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    if not token:
        raise DispatcherError(
            "Telegram token is missing. Set PROPERTY_DISPATCHER_TELEGRAM_TOKEN "
            "or TELEGRAM_BOT_TOKEN in the service Hermes profile."
        )
    return token


def _chunks(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    text = str(text).strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    result: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = paragraph if not current else current + "\n\n" + paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            result.append(current)
            current = ""
        while len(paragraph) > limit:
            result.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = paragraph
    if current:
        result.append(current)
    return result


def send_telegram(token: str, chat_id: str, text: str, timeout: int = 20) -> None:
    """Send plain-text messages directly through the service Telegram bot."""

    chat_id = str(chat_id or "").strip()
    if not chat_id:
        raise DispatcherError("follow has no telegram.chat_id")
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in _chunks(text):
        body = json.dumps(
            {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise DispatcherError(f"Telegram HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise DispatcherError(f"Telegram delivery failed: {exc}") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise DispatcherError(f"Telegram rejected message: {payload!r}")


def _llm_prompt(manifest: dict[str, Any], events: list[dict[str, Any]]) -> str:
    custom = str(manifest.get("llm_prompt") or "").strip()
    instruction = custom or (
        "Analyse ces nouvelles annonces et rédige uniquement le message Telegram final à envoyer. "
        "Sois concis. Utilise uniquement les données fournies. N'invente rien. "
        "Ne recherche aucune autre annonce, ne rescane aucun site et n'utilise aucun navigateur. "
        "Pour chaque annonce, indique au minimum le site, le titre, le prix, la surface, "
        "la localisation et l'URL lorsque ces valeurs sont disponibles."
    )
    public_manifest = {
        "follow_id": manifest.get("id"),
        "name": manifest.get("name"),
    }
    return (
        instruction
        + "\n\nContexte du suivi :\n"
        + json.dumps(public_manifest, ensure_ascii=False, indent=2)
        + "\n\nNouvelles annonces :\n"
        + json.dumps(events, ensure_ascii=False, indent=2)
    )


def run_hermes_llm(
    follow_dir: Path,
    manifest: dict[str, Any],
    events: list[dict[str, Any]],
    timeout: int = DEFAULT_LLM_TIMEOUT,
) -> str:
    """Run one Hermes one-shot in the current service profile."""

    prompt_path = follow_dir / f".dispatcher_llm_{uuid.uuid4().hex}.txt"
    prompt_path.write_text(_llm_prompt(manifest, events), encoding="utf-8")
    executable = str(os.getenv("HERMES_EXECUTABLE") or "hermes")
    cmd = [executable, "chat", "--query-file", str(prompt_path)]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except FileNotFoundError as exc:
        raise DispatcherError("Hermes executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise DispatcherError("Hermes LLM one-shot timed out") from exc
    finally:
        prompt_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Hermes one-shot failed").strip()[-2000:]
        raise DispatcherError(detail)
    text = ANSI_RE.sub("", proc.stdout or "").strip()
    if not text:
        raise DispatcherError("Hermes LLM returned empty output")
    return text


def _pending_message(
    follow_dir: Path,
    manifest: dict[str, Any],
    pending: dict[str, Any],
    logger: logging.Logger,
) -> dict[str, Any]:
    if str(pending.get("message") or "").strip():
        return pending

    events = pending.get("events") or []
    if not isinstance(events, list):
        events = []
    if bool(pending.get("use_llm")):
        try:
            pending["message"] = run_hermes_llm(follow_dir, manifest, events)
            pending["llm_used"] = True
        except Exception as exc:
            pending["llm_used"] = False
            pending["llm_error"] = str(exc)[:1000]
            pending["message"] = follows._format_new_message(manifest, events)
            logger.error("follow=%s LLM failed; deterministic fallback used: %s", manifest.get("id"), exc)
    else:
        pending["message"] = follows._format_new_message(manifest, events)
        pending["llm_used"] = False

    _write_json(_pending_path(follow_dir), pending)
    return pending


def deliver_pending(
    follow_dir: Path,
    manifest: dict[str, Any],
    token: str,
    logger: logging.Logger,
) -> bool:
    path = _pending_path(follow_dir)
    if not path.exists():
        return True
    pending = _read_json(path)
    pending = _pending_message(follow_dir, manifest, pending, logger)
    pending["attempts"] = int(pending.get("attempts") or 0) + 1
    _write_json(path, pending)
    chat_id = str((manifest.get("telegram") or {}).get("chat_id") or "")
    try:
        send_telegram(token, chat_id, str(pending.get("message") or ""))
    except Exception as exc:
        pending["last_delivery_error"] = str(exc)[:1000]
        _write_json(path, pending)
        logger.error("follow=%s pending Telegram delivery failed: %s", manifest.get("id"), exc)
        return False
    path.unlink(missing_ok=True)
    return True


def _write_pending(
    follow_dir: Path,
    manifest: dict[str, Any],
    events: list[dict[str, Any]],
) -> None:
    _write_json(
        _pending_path(follow_dir),
        {
            "created_at": now_local().isoformat(timespec="seconds"),
            "follow_id": manifest.get("id"),
            "events": events,
            "use_llm": bool(manifest.get("run_llm_on_new", False)),
            "message": None,
            "llm_used": False,
            "attempts": 0,
        },
    )


def _result(
    follow_id: str,
    status: str,
    *,
    new_count: int = 0,
    llm_used: bool = False,
    notification_sent: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "follow_id": follow_id,
        "status": status,
        "new_count": new_count,
        "llm_used": llm_used,
        "notification_sent": notification_sent,
        "error": error,
    }


def process_follow(
    root: Path,
    follow_dir: Path,
    token: str,
    logger: logging.Logger,
    *,
    scan_timeout: int = DEFAULT_SCAN_TIMEOUT,
    retry_minutes: int = DEFAULT_RETRY_MINUTES,
    force: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest(follow_dir)
    follow_id = str(manifest.get("id") or follow_dir.name)
    state = load_dispatch_state(follow_dir)
    now = now_local()

    if not bool(manifest.get("enabled", True)):
        return _result(follow_id, "DISABLED")

    # Delivery is retried before any new scan. This prevents a Telegram outage
    # from losing an event that the deterministic watcher already persisted.
    if _pending_path(follow_dir).exists():
        if not deliver_pending(follow_dir, manifest, token, logger):
            state["last_error"] = "pending Telegram delivery failed"
            state["failure_streak"] = int(state.get("failure_streak") or 0) + 1
            save_dispatch_state(follow_dir, state)
            return _result(follow_id, "PENDING_DELIVERY", error=state["last_error"])
        logger.info("follow=%s pending delivery recovered", follow_id)

    if not force and not is_due(manifest, state, now):
        return _result(follow_id, "NOT_DUE")

    state["last_run_at"] = now.isoformat(timespec="seconds")
    try:
        scanned_dir, scanned_manifest, events, source_errors = follows._scan_follow(
            root,
            follow_id,
            timeout=scan_timeout,
        )
        tick = follows._tick_payload(scanned_manifest, events, source_errors)
        follows._write_tick(scanned_dir, tick)

        notification_sent = False
        llm_used = False
        if events:
            # Persist before any LLM or Telegram side effect. If the process dies
            # afterwards, the next dispatcher tick resumes from this outbox.
            _write_pending(follow_dir, scanned_manifest, events)
            delivered = deliver_pending(follow_dir, scanned_manifest, token, logger)
            if delivered:
                notification_sent = True
                llm_used = bool(scanned_manifest.get("run_llm_on_new", False))
            else:
                state["last_error"] = "Telegram delivery pending"
        elif bool(scanned_manifest.get("notify_every_run", False)):
            heartbeat = follows._format_heartbeat(scanned_manifest, source_errors)
            send_telegram(
                token,
                str((scanned_manifest.get("telegram") or {}).get("chat_id") or ""),
                heartbeat,
            )
            notification_sent = True

        state["next_run_at"] = _next_run(scanned_manifest, now).isoformat(timespec="seconds")
        state["last_success_at"] = now_local().isoformat(timespec="seconds")
        state["failure_streak"] = 0
        state["last_error"] = (
            f"{source_errors} source(s) en erreur" if source_errors else None
        )
        save_dispatch_state(follow_dir, state)
        status = "NEW" if events else "NO_CHANGE"
        logger.info(
            "follow=%s status=%s new=%d source_errors=%d llm=%s notified=%s",
            follow_id,
            status,
            len(events),
            source_errors,
            llm_used,
            notification_sent,
        )
        return _result(
            follow_id,
            status,
            new_count=len(events),
            llm_used=llm_used,
            notification_sent=notification_sent,
            error=state["last_error"],
        )
    except Exception as exc:
        state["next_run_at"] = (
            now + timedelta(minutes=max(1, int(retry_minutes)))
        ).isoformat(timespec="seconds")
        state["last_error"] = str(exc)[:1000]
        state["failure_streak"] = int(state.get("failure_streak") or 0) + 1
        save_dispatch_state(follow_dir, state)
        logger.exception("follow=%s dispatcher failure", follow_id)
        return _result(follow_id, "ERROR", error=state["last_error"])


class DispatcherLock:
    def __init__(self, path: Path, stale_seconds: int = LOCK_STALE_SECONDS):
        self.path = path
        self.stale_seconds = stale_seconds
        self.owned = False

    def __enter__(self) -> "DispatcherLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                age = max(0.0, datetime.now().timestamp() - self.path.stat().st_mtime)
            except OSError:
                age = 0.0
            if age > self.stale_seconds:
                self.path.unlink(missing_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise DispatcherAlreadyRunning("dispatcher already running") from exc
        try:
            os.write(fd, f"pid={os.getpid()}\nstarted={now_local().isoformat()}\n".encode("ascii", errors="ignore"))
        finally:
            os.close(fd)
        self.owned = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.owned:
            self.path.unlink(missing_ok=True)
            self.owned = False


def configure_logging(root: Path, verbose: bool = False) -> logging.Logger:
    root.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("property_dispatcher")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(root / "dispatcher.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if verbose:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        logger.addHandler(stream)
    return logger


def run_dispatcher(
    root: Path,
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    scan_timeout: int = DEFAULT_SCAN_TIMEOUT,
    retry_minutes: int = DEFAULT_RETRY_MINUTES,
    follow_id: str | None = None,
    force: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    token = _telegram_token()
    logger = configure_logging(root, verbose=verbose)
    lock_path = root / "dispatcher.lock"

    try:
        with DispatcherLock(lock_path):
            follow_dirs = list_follow_dirs(root)
            if follow_id:
                wanted = follows.normalize_id(follow_id)
                follow_dirs = [path for path in follow_dirs if path.name == wanted]
                if not follow_dirs:
                    raise DispatcherError(f"unknown follow: {wanted}")

            due: list[Path] = []
            skipped = 0
            for follow_dir in follow_dirs:
                try:
                    manifest = load_manifest(follow_dir)
                    state = load_dispatch_state(follow_dir)
                    pending = _pending_path(follow_dir).exists()
                    if force or pending or is_due(manifest, state, now_local()):
                        due.append(follow_dir)
                    else:
                        skipped += 1
                except Exception as exc:
                    logger.error("follow_dir=%s cannot be scheduled: %s", follow_dir, exc)

            results: list[dict[str, Any]] = []
            workers = max(1, min(int(max_workers), 32))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="property-follow") as pool:
                futures = {
                    pool.submit(
                        process_follow,
                        root,
                        follow_dir,
                        token,
                        logger,
                        scan_timeout=scan_timeout,
                        retry_minutes=retry_minutes,
                        force=force,
                    ): follow_dir
                    for follow_dir in due
                }
                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        path = futures[future]
                        logger.exception("unhandled worker failure for %s", path)
                        results.append(_result(path.name, "ERROR", error=str(exc)[:1000]))

            results.sort(key=lambda item: str(item.get("follow_id") or ""))
            return {
                "status": "OK",
                "timestamp": now_local().isoformat(timespec="seconds"),
                "registered": len(follow_dirs),
                "due": len(due),
                "skipped_not_due": skipped,
                "new_follows": sum(1 for item in results if item["status"] == "NEW"),
                "llm_calls": sum(1 for item in results if item.get("llm_used")),
                "notifications": sum(1 for item in results if item.get("notification_sent")),
                "errors": sum(1 for item in results if item["status"] in {"ERROR", "PENDING_DELIVERY"}),
                "results": results,
            }
    except DispatcherAlreadyRunning:
        return {
            "status": "ALREADY_RUNNING",
            "timestamp": now_local().isoformat(timespec="seconds"),
        }


def dispatcher_status(root: Path) -> dict[str, Any]:
    follows_status: list[dict[str, Any]] = []
    for follow_dir in list_follow_dirs(root):
        try:
            manifest = load_manifest(follow_dir)
            state = load_dispatch_state(follow_dir)
            follows_status.append(
                {
                    "follow_id": manifest.get("id") or follow_dir.name,
                    "name": manifest.get("name"),
                    "enabled": bool(manifest.get("enabled", True)),
                    "interval_minutes": follow_interval_minutes(manifest),
                    "next_run_at": state.get("next_run_at"),
                    "last_success_at": state.get("last_success_at"),
                    "failure_streak": int(state.get("failure_streak") or 0),
                    "pending_delivery": _pending_path(follow_dir).exists(),
                    "notify_every_run": bool(manifest.get("notify_every_run", False)),
                    "run_llm_on_new": bool(manifest.get("run_llm_on_new", False)),
                }
            )
        except Exception as exc:
            follows_status.append({"follow_id": follow_dir.name, "error": str(exc)[:1000]})
    return {
        "status": "OK",
        "registered": len(follows_status),
        "follows": follows_status,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="property-dispatcher")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run all due follows")
    run.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    run.add_argument("--scan-timeout", type=int, default=DEFAULT_SCAN_TIMEOUT)
    run.add_argument("--retry-minutes", type=int, default=DEFAULT_RETRY_MINUTES)
    run.add_argument("--follow", help="run or inspect only one follow id")
    run.add_argument("--force", action="store_true", help="ignore next_run_at")
    run.add_argument("--verbose", action="store_true")

    sub.add_parser("status", help="show dispatcher scheduling state")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    try:
        if args.command == "status":
            payload = dispatcher_status(root)
        else:
            payload = run_dispatcher(
                root,
                max_workers=args.max_workers,
                scan_timeout=args.scan_timeout,
                retry_minutes=args.retry_minutes,
                follow_id=args.follow,
                force=args.force,
                verbose=args.verbose,
            )
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0 if payload.get("status") in {"OK", "ALREADY_RUNNING"} else 1
    except Exception as exc:
        print(
            json.dumps(
                {"status": "ERROR", "error": str(exc)[:1000]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
