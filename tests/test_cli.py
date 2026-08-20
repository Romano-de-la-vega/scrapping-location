from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from watcher import cli
from watcher.config import AppConfig, config_from_dict
from watcher.database import Database
from watcher.locks import AlreadyRunningError


def app_config(tmp_path: Path, *sites: str) -> AppConfig:
    selected = sites or ("seloger",)
    return config_from_dict(
        {
            "database_path": "missing/state.db",
            "log_path": "logs/watcher.log",
            "lock_path": "locks/watcher.lock",
            "browser": {"headless": True},
            "sites": {
                site: {"search_url": f"https://search.test/{site}"}
                for site in selected
            },
        },
        base_dir=tmp_path,
    )


def payload_from_stdout(stdout: str) -> dict[str, Any]:
    assert stdout.count("\n") == 1
    return json.loads(stdout)


class RecordingLock:
    def __init__(self, _path: Path, trace: list[str]) -> None:
        self.trace = trace

    def __enter__(self) -> "RecordingLock":
        self.trace.append("lock")
        return self

    def __exit__(self, *_args: object) -> None:
        self.trace.append("unlock")


def quiet_logging(
    monkeypatch: Any, *, calls: list[tuple[Path, bool]] | None = None
) -> logging.Logger:
    logger = logging.getLogger("watcher.cli.tests")

    def configure(path: Path, verbose: bool = False) -> logging.Logger:
        if calls is not None:
            calls.append((path, verbose))
        return logger

    monkeypatch.setattr(cli, "configure_logging", configure)
    return logger


def test_run_emits_exactly_one_json_line_and_passes_cli_options(
    tmp_path, monkeypatch, capsys
) -> None:
    config = app_config(tmp_path, "leboncoin", "seloger")
    trace: list[str] = []
    logging_calls: list[tuple[Path, bool]] = []
    loaded_paths: list[Path] = []

    def load(path: Path) -> AppConfig:
        loaded_paths.append(path)
        return config

    async def fake_run_watcher(
        received_config: AppConfig, **kwargs: Any
    ) -> dict[str, Any]:
        trace.append("runner")
        assert received_config is config
        assert kwargs["site_names"] == "seloger"
        assert kwargs["dry_run"] is True
        assert kwargs["logger"].name == "watcher.cli.tests"
        return {"status": "NO_CHANGE", "actionable_count": 0}

    monkeypatch.setattr(cli, "load_config", load)
    quiet_logging(monkeypatch, calls=logging_calls)
    monkeypatch.setattr(
        cli, "GlobalLock", lambda path: RecordingLock(path, trace)
    )
    monkeypatch.setattr(cli, "run_watcher", fake_run_watcher)

    result = cli.main(
        [
            "run",
            "--config",
            "custom.json",
            "--site",
            "seloger",
            "--dry-run",
            "--verbose",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    assert payload_from_stdout(captured.out) == {
        "status": "NO_CHANGE",
        "actionable_count": 0,
    }
    assert loaded_paths == [Path("custom.json")]
    assert logging_calls == [(config.log_path, True)]
    assert trace == ["lock", "runner", "unlock"]


def test_already_running_returns_json_without_calling_runner(
    tmp_path, monkeypatch, capsys
) -> None:
    config = app_config(tmp_path)
    runner_called = False

    class BusyLock:
        def __init__(self, _path: Path) -> None:
            pass

        def __enter__(self) -> None:
            raise AlreadyRunningError("busy")

        def __exit__(self, *_args: object) -> None:
            return None

    async def forbidden_runner(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal runner_called
        runner_called = True
        return {}

    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    quiet_logging(monkeypatch)
    monkeypatch.setattr(cli, "GlobalLock", BusyLock)
    monkeypatch.setattr(cli, "run_watcher", forbidden_runner)

    assert cli.main(["run"]) == 0
    captured = capsys.readouterr()
    assert captured.out == '{"status":"ALREADY_RUNNING"}\n'
    assert captured.err == ""
    assert runner_called is False


def test_status_of_absent_database_is_empty_and_does_not_create_file(
    tmp_path, monkeypatch, capsys
) -> None:
    config = app_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    quiet_logging(monkeypatch)

    assert not config.database_path.exists()
    assert cli.main(["status", "--config", "ignored.json"]) == 0

    result = payload_from_stdout(capsys.readouterr().out)
    assert result == {"status": "OK", "sites": {}, "last_run": None}
    assert not config.database_path.exists()
    assert not config.database_path.parent.exists()


def test_history_uses_limit_and_serializes_recent_events(
    tmp_path, monkeypatch, capsys
) -> None:
    config = app_config(tmp_path)
    with Database(config.database_path) as database:
        with database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO events (
                    run_id, site, listing_id, event_type,
                    before_json, after_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "run-1",
                        "seloger",
                        "A",
                        "UPDATED",
                        '{"price_eur":750}',
                        '{"price_eur":730}',
                        "2026-08-20T08:00:00+02:00",
                    ),
                    (
                        "run-2",
                        "seloger",
                        "B",
                        "NEW",
                        None,
                        '{"price_eur":700}',
                        "2026-08-20T08:30:00+02:00",
                    ),
                ],
            )

    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    quiet_logging(monkeypatch)

    assert cli.main(["history", "--limit", "1"]) == 0
    result = payload_from_stdout(capsys.readouterr().out)
    assert result["status"] == "OK"
    assert result["limit"] == 1
    assert len(result["events"]) == 1
    assert result["events"][0]["run_id"] == "run-2"
    assert result["events"][0]["after"] == {"price_eur": 700}


def test_diagnose_reports_challenge_without_scanning_or_dumping_dom(
    tmp_path, monkeypatch, capsys
) -> None:
    config = app_config(tmp_path, "seloger")
    trace: list[str] = []

    class FakePage:
        url = "about:blank"

        async def title(self) -> str:
            return "Security challenge"

    page = FakePage()

    class PageContext:
        async def __aenter__(self) -> FakePage:
            return page

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeBrowserSession:
        def __init__(self, browser: Any, scan: Any) -> None:
            assert browser is config.browser
            assert scan is config.scan
            trace.append("browser")

        async def __aenter__(self) -> "FakeBrowserSession":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def page(self) -> PageContext:
            return PageContext()

        async def navigate(self, target: FakePage, url: str) -> None:
            trace.append("navigate")
            target.url = url

        async def challenge_reason(self, _target: FakePage) -> str:
            return "security challenge"

    class FakeAdapter:
        search_url = "https://search.test/seloger"

        async def scan_results(self, _page: FakePage) -> list[Any]:
            raise AssertionError("a challenge page must not be scanned")

    def adapter_factory(site: str, url: str) -> FakeAdapter:
        assert site == "seloger"
        assert url == config.sites["seloger"].search_url
        trace.append("adapter")
        return FakeAdapter()

    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    quiet_logging(monkeypatch)
    monkeypatch.setattr(
        cli, "GlobalLock", lambda path: RecordingLock(path, trace)
    )
    monkeypatch.setattr(cli, "BrowserSession", FakeBrowserSession)
    monkeypatch.setattr(cli, "create_adapter", adapter_factory)

    assert cli.main(["diagnose", "--site", "seloger"]) == 0
    captured = capsys.readouterr()
    result = payload_from_stdout(captured.out)
    assert result == {
        "status": "CHALLENGE",
        "site": "seloger",
        "loaded_url": "https://search.test/seloger",
        "page_title": "Security challenge",
        "candidate_links": 0,
        "valid_ids": 0,
        "listings": 0,
        "duplicates": 0,
        "rejected_candidates": 0,
        "challenge": True,
        "challenge_reason": "security challenge",
    }
    assert "<html" not in captured.out.lower()
    assert trace == ["lock", "adapter", "browser", "navigate", "unlock"]


def test_missing_configuration_is_a_stable_json_error(tmp_path, capsys) -> None:
    missing = tmp_path / "does-not-exist.json"

    assert cli.main(["status", "--config", str(missing)]) == 2
    captured = capsys.readouterr()
    result = payload_from_stdout(captured.out)
    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "CONFIG_ERROR"
    assert str(missing) in result["error"]["message"]
    assert "Traceback" not in captured.out


def test_invalid_arguments_are_a_json_usage_error(capsys) -> None:
    assert cli.main(["history", "--limit", "0"]) == 2
    captured = capsys.readouterr()
    result = payload_from_stdout(captured.out)
    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "USAGE_ERROR"
    assert "at least 1" in result["error"]["message"]
    assert captured.err == ""
