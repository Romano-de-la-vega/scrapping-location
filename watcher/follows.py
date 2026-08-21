"""Independent multi-user property follows with Hermes no-agent delivery."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT_ROOT / "data" / "follows"
DEFAULT_BASE_CONFIG = PROJECT_ROOT / "config.json"

SITE_DOMAINS = {
    "leboncoin.fr": "leboncoin",
    "seloger.com": "seloger",
    "seventee.com": "seventee",
}


class FollowError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FollowError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FollowError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise FollowError(f"JSON root must be an object: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def normalize_id(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
    if not value:
        raise FollowError("follow id must contain at least one letter or digit")
    return value[:60]


def detect_site(url: str) -> str:
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FollowError(f"invalid search URL: {url}")
    host = parsed.hostname.lower().removeprefix("www.")
    for domain, site in SITE_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return site
    raise FollowError(
        f"unsupported site: {host}. Supported adapters: {', '.join(SITE_DOMAINS.values())}"
    )


def _criteria(base: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    criteria = dict(base.get("criteria") or {})
    if args.postal_code:
        criteria["postal_codes"] = [str(v).strip() for v in args.postal_code]
    for key in ("price_min", "price_max", "surface_min", "surface_max"):
        value = getattr(args, key, None)
        if value is not None:
            criteria[key] = value
    if not criteria.get("postal_codes"):
        raise FollowError("at least one --postal-code is required")
    return criteria


def _source_config(base: dict[str, Any], site: str, url: str, criteria: dict[str, Any]) -> dict[str, Any]:
    return {
        "timezone": base.get("timezone", "Europe/Paris"),
        "database_path": "state.db",
        "log_path": "watcher.log",
        "lock_path": "watcher.lock",
        "debug_directory": "debug",
        "debug_artifacts_on_error": bool(base.get("debug_artifacts_on_error", True)),
        "browser": dict(base.get("browser") or {}),
        "criteria": criteria,
        "diff": dict(base.get("diff") or {}),
        "scan": dict(base.get("scan") or {}),
        "sites": {site: {"enabled": True, "search_url": url}},
    }


def create_follow(
    root: Path,
    follow_id: str,
    name: str,
    chat_id: str,
    schedule: str,
    urls: Iterable[str],
    base_config: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    follow_id = normalize_id(follow_id)
    follow_dir = root / follow_id
    manifest_path = follow_dir / "follow.json"
    if manifest_path.exists():
        raise FollowError(f"follow already exists: {follow_id}")

    base = _read_json(base_config)
    criteria = _criteria(base, args)
    counts: dict[str, int] = {}
    sources: list[dict[str, Any]] = []
    clean_urls = [str(url).strip() for url in urls if str(url).strip()]
    if not clean_urls:
        raise FollowError("at least one --url is required")

    for url in clean_urls:
        site = detect_site(url)
        counts[site] = counts.get(site, 0) + 1
        source_id = f"{site}-{counts[site]}"
        config_path = follow_dir / "sources" / source_id / "config.json"
        _write_json(config_path, _source_config(base, site, url, criteria))
        sources.append({
            "id": source_id,
            "site": site,
            "url": url,
            "config": config_path.relative_to(follow_dir).as_posix(),
        })

    manifest = {
        "version": 1,
        "id": follow_id,
        "name": str(name).strip() or follow_id,
        "enabled": True,
        "mode": "notify_only",
        "schedule": str(schedule).strip() or "every 5m",
        "telegram": {"chat_id": str(chat_id).strip()},
        "sources": sources,
        "hermes": {
            "job_name": f"Immo follow - {str(name).strip() or follow_id}",
            "job_id": None,
            "script": f"property_follow_{follow_id}.py",
        },
    }
    if not manifest["telegram"]["chat_id"]:
        raise FollowError("telegram chat id must not be empty")
    _write_json(manifest_path, manifest)
    return manifest


def load_follow(root: Path, follow_id: str) -> tuple[Path, dict[str, Any]]:
    follow_dir = root / normalize_id(follow_id)
    return follow_dir, _read_json(follow_dir / "follow.json")


def _actionable(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key in ("new", "became_eligible"):
        values = payload.get(key) or []
        if not isinstance(values, list):
            continue
        result.extend(v for v in values if isinstance(v, dict) and bool(v.get("actionable", True)))
    return result


def _event_key(event: dict[str, Any]) -> tuple[str, str]:
    return str(event.get("site") or "unknown"), str(event.get("id") or event.get("url") or event)


def _format_event(event: dict[str, Any]) -> str:
    title = event.get("title") or "Annonce immobilière"
    site = str(event.get("site") or "site").capitalize()
    details: list[str] = []
    if event.get("price_eur") is not None:
        details.append(f"{event['price_eur']} €")
    if event.get("surface_m2") is not None:
        details.append(f"{event['surface_m2']} m²")
    location = event.get("location") or event.get("postal_code")
    if location:
        details.append(str(location))
    line = f"• [{site}] {title}"
    if details:
        line += " — " + " | ".join(details)
    if event.get("url"):
        line += "\n  " + str(event["url"])
    return line


def run_follow(root: Path, follow_id: str, timeout: int = 120) -> str:
    follow_dir, manifest = load_follow(root, follow_id)
    if not manifest.get("enabled", True):
        return ""

    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in manifest.get("sources") or []:
        config_path = (follow_dir / source["config"]).resolve()
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "watcher", "run", "--config", str(config_path)],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            continue
        if proc.returncode != 0:
            continue
        try:
            payload = json.loads(proc.stdout.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("status") == "ERROR":
            continue
        for event in _actionable(payload):
            key = _event_key(event)
            if key not in seen:
                seen.add(key)
                events.append(event)

    if not events:
        return ""
    name = str(manifest.get("name") or follow_id)
    return f"🏠 {name} — {len(events)} nouvelle(s) annonce(s)\n\n" + "\n\n".join(_format_event(e) for e in events)


def _hermes_home() -> Path:
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"]).expanduser().resolve()
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "hermes"
    return Path.home() / ".hermes"


def _script_content(root: Path, follow_id: str) -> str:
    return f'''import subprocess\n\nPYTHON = {sys.executable!r}\nROOT = {str(root.resolve())!r}\nFOLLOW = {normalize_id(follow_id)!r}\nPROJECT = {str(PROJECT_ROOT.resolve())!r}\n\np = subprocess.run(\n    [PYTHON, "-m", "watcher.follows", "--root", ROOT, "run", FOLLOW],\n    cwd=PROJECT, capture_output=True, text=True, timeout=300\n)\nif p.returncode == 0 and p.stdout.strip():\n    print(p.stdout.strip())\n'''


def _hermes_jobs(home: Path) -> list[dict[str, Any]]:
    path = home / "cron" / "jobs.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    return [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []


def install_follow(root: Path, follow_id: str) -> dict[str, Any]:
    follow_dir, manifest = load_follow(root, follow_id)
    home = _hermes_home()
    scripts = home / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    meta = dict(manifest.get("hermes") or {})
    script_name = str(meta.get("script") or f"property_follow_{normalize_id(follow_id)}.py")
    (scripts / script_name).write_text(_script_content(root, follow_id), encoding="utf-8")

    existing = meta.get("job_id")
    if existing and any(job.get("id") == existing and job.get("enabled", True) for job in _hermes_jobs(home)):
        raise FollowError(f"follow already installed as Hermes job {existing}")

    chat_id = str((manifest.get("telegram") or {}).get("chat_id") or "").strip()
    job_name = str(meta.get("job_name") or f"Immo follow - {follow_id}")
    cmd = [
        "hermes", "cron", "create", str(manifest.get("schedule") or "every 5m"),
        "--no-agent", "--script", script_name,
        "--deliver", f"telegram:{chat_id}", "--name", job_name,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise FollowError("Hermes executable not found on PATH") from exc
    if proc.returncode != 0:
        raise FollowError((proc.stderr or proc.stdout or "Hermes cron create failed").strip()[-1000:])

    matches = [job for job in _hermes_jobs(home) if job.get("name") == job_name]
    if matches:
        matches.sort(key=lambda job: str(job.get("created_at") or ""))
        meta["job_id"] = matches[-1].get("id")
    meta["script"] = script_name
    meta["job_name"] = job_name
    manifest["hermes"] = meta
    _write_json(follow_dir / "follow.json", manifest)
    return {
        "status": "INSTALLED",
        "follow": manifest["id"],
        "job_id": meta.get("job_id"),
        "schedule": manifest["schedule"],
        "deliver": f"telegram:{chat_id}",
        "mode": "no-agent",
    }


def uninstall_follow(root: Path, follow_id: str) -> dict[str, Any]:
    follow_dir, manifest = load_follow(root, follow_id)
    meta = dict(manifest.get("hermes") or {})
    job_id = meta.get("job_id")
    if job_id:
        proc = subprocess.run(["hermes", "cron", "remove", str(job_id)], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise FollowError((proc.stderr or proc.stdout or "Hermes cron remove failed").strip()[-1000:])
    if meta.get("script"):
        (_hermes_home() / "scripts" / str(meta["script"])).unlink(missing_ok=True)
    meta["job_id"] = None
    manifest["hermes"] = meta
    _write_json(follow_dir / "follow.json", manifest)
    return {"status": "UNINSTALLED", "follow": manifest["id"]}


def list_follows(root: Path) -> list[dict[str, Any]]:
    result = []
    for path in sorted(root.glob("*/follow.json")) if root.exists() else []:
        manifest = _read_json(path)
        result.append({
            "id": manifest.get("id"), "name": manifest.get("name"),
            "schedule": manifest.get("schedule"),
            "telegram_chat_id": (manifest.get("telegram") or {}).get("chat_id"),
            "sources": len(manifest.get("sources") or []),
            "hermes_job_id": (manifest.get("hermes") or {}).get("job_id"),
            "mode": manifest.get("mode"),
        })
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="property-follow")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add")
    add.add_argument("follow_id")
    add.add_argument("--name", required=True)
    add.add_argument("--telegram-chat", required=True)
    add.add_argument("--schedule", default="every 5m")
    add.add_argument("--url", action="append", required=True)
    add.add_argument("--base-config", type=Path, default=DEFAULT_BASE_CONFIG)
    add.add_argument("--postal-code", action="append")
    add.add_argument("--price-min", type=int)
    add.add_argument("--price-max", type=int)
    add.add_argument("--surface-min", type=float)
    add.add_argument("--surface-max", type=float)
    add.add_argument("--install", action="store_true")

    for command in ("install", "uninstall", "run"):
        p = sub.add_parser(command)
        p.add_argument("follow_id")
    sub.add_parser("list")
    delete = sub.add_parser("delete")
    delete.add_argument("follow_id")
    delete.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    try:
        if args.command == "add":
            manifest = create_follow(
                root, args.follow_id, args.name, args.telegram_chat, args.schedule,
                args.url, args.base_config.expanduser().resolve(), args,
            )
            result: dict[str, Any] = {"status": "CREATED", "follow": manifest}
            if args.install:
                result["hermes"] = install_follow(root, args.follow_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "install":
            print(json.dumps(install_follow(root, args.follow_id), ensure_ascii=False, indent=2))
        elif args.command == "uninstall":
            print(json.dumps(uninstall_follow(root, args.follow_id), ensure_ascii=False, indent=2))
        elif args.command == "run":
            message = run_follow(root, args.follow_id)
            if message:
                print(message)
        elif args.command == "list":
            print(json.dumps({"follows": list_follows(root)}, ensure_ascii=False, indent=2))
        else:
            follow_dir, manifest = load_follow(root, args.follow_id)
            if (manifest.get("hermes") or {}).get("job_id") and not args.force:
                raise FollowError("follow is still installed; run uninstall first or use --force")
            shutil.rmtree(follow_dir)
            print(json.dumps({"status": "DELETED", "follow": manifest["id"]}))
        return 0
    except (FollowError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "ERROR", "message": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
