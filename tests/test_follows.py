import argparse
import json

import pytest

from watcher import follows


def _base_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "timezone": "Europe/Paris",
                "browser": {"mode": "cdp", "cdp_url": "http://127.0.0.1:9224"},
                "criteria": {
                    "postal_codes": ["69006"],
                    "price_min": 550,
                    "price_max": 800,
                    "surface_min": 30,
                    "surface_max": 60,
                },
                "diff": {"missing_threshold": 2},
                "scan": {"max_pages_per_site": 1},
            }
        ),
        encoding="utf-8",
    )
    return path


def _args(**values):
    defaults = {
        "postal_code": None,
        "price_min": None,
        "price_max": None,
        "surface_min": None,
        "surface_max": None,
        "notify_every_run": False,
        "run_llm_on_new": False,
    }
    defaults.update(values)
    return argparse.Namespace(**defaults)


def test_detect_supported_sites():
    assert follows.detect_site("https://www.leboncoin.fr/recherche?x=1") == "leboncoin"
    assert follows.detect_site("https://www.seloger.com/foo") == "seloger"
    assert follows.detect_site("https://candidate.seventee.com/foo") == "seventee"


def test_reject_unknown_site():
    with pytest.raises(follows.FollowError):
        follows.detect_site("https://example.com/search")


def test_create_follow_supports_multiple_urls_per_site(tmp_path):
    root = tmp_path / "follows"
    manifest = follows.create_follow(
        root,
        "Paul Lyon",
        "Paul - Lyon",
        "123456",
        "every 5m",
        [
            "https://www.leboncoin.fr/recherche?a=1",
            "https://www.leboncoin.fr/recherche?a=2",
            "https://www.seloger.com/foo",
        ],
        _base_config(tmp_path),
        _args(postal_code=["69003"], price_max=900),
    )

    assert manifest["id"] == "paul-lyon"
    assert manifest["notify_every_run"] is False
    assert manifest["run_llm_on_new"] is False
    assert [source["id"] for source in manifest["sources"]] == [
        "leboncoin-1",
        "leboncoin-2",
        "seloger-1",
    ]

    for source in manifest["sources"]:
        config_path = root / "paul-lyon" / source["config"]
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config["database_path"] == "state.db"
        assert config["lock_path"] == "watcher.lock"
        assert config["criteria"]["postal_codes"] == ["69003"]
        assert config["criteria"]["price_max"] == 900


def test_create_follow_persists_behavior_flags(tmp_path):
    root = tmp_path / "follows"
    manifest = follows.create_follow(
        root,
        "alice",
        "Alice",
        "987654",
        "every 10m",
        ["https://www.seloger.com/foo"],
        _base_config(tmp_path),
        _args(notify_every_run=True, run_llm_on_new=True),
    )
    assert manifest["notify_every_run"] is True
    assert manifest["run_llm_on_new"] is True
    assert manifest["mode"] == "llm_on_new"


def test_actionable_events_exclude_rejected():
    payload = {
        "new": [
            {"site": "leboncoin", "id": "1", "actionable": True},
            {"site": "leboncoin", "id": "2", "actionable": False},
        ],
        "became_eligible": [{"site": "seloger", "id": "3", "actionable": True}],
    }
    assert [event["id"] for event in follows._actionable(payload)] == ["1", "3"]


def test_generated_hermes_scripts_are_valid_python(tmp_path):
    for command in ("run", "gate", "audit"):
        text = follows._script_content(tmp_path, "paul", command)
        compile(text, f"<generated-{command}-script>", "exec")
        assert "watcher.follows" in text
        assert command in text


def test_gate_only_wakes_when_events_exist(monkeypatch, tmp_path):
    root = tmp_path / "follows"
    follow_dir = root / "paul"
    follow_dir.mkdir(parents=True)
    (follow_dir / "follow.json").write_text(
        json.dumps({"id": "paul", "name": "Paul", "run_llm_on_new": True}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        follows,
        "_scan_follow",
        lambda *args, **kwargs: (follow_dir, {"id": "paul", "name": "Paul", "run_llm_on_new": True}, [], 0),
    )
    assert follows.gate_follow(root, "paul") == {"wakeAgent": False}

    event = {"site": "leboncoin", "id": "1", "url": "https://example.test/1", "actionable": True}
    monkeypatch.setattr(
        follows,
        "_scan_follow",
        lambda *args, **kwargs: (follow_dir, {"id": "paul", "name": "Paul", "run_llm_on_new": True}, [event], 0),
    )
    result = follows.gate_follow(root, "paul")
    assert result["wakeAgent"] is True
    assert result["context"]["property_follow"]["events"] == [event]


def test_audit_emits_once_per_tick(tmp_path):
    root = tmp_path / "follows"
    follow_dir = root / "paul"
    follow_dir.mkdir(parents=True)
    (follow_dir / "follow.json").write_text(
        json.dumps({"id": "paul", "name": "Paul"}), encoding="utf-8"
    )
    (follow_dir / "last_run.json").write_text(
        json.dumps(
            {
                "tick_id": "abc",
                "timestamp": "2026-08-21T21:00:00+02:00",
                "new_count": 0,
                "source_errors": 0,
                "llm_wake": False,
            }
        ),
        encoding="utf-8",
    )
    first = follows.audit_follow(root, "paul")
    second = follows.audit_follow(root, "paul")
    assert "LLM : non exécuté" in first
    assert second == ""
