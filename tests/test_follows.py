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


def test_actionable_events_exclude_rejected():
    payload = {
        "new": [
            {"site": "leboncoin", "id": "1", "actionable": True},
            {"site": "leboncoin", "id": "2", "actionable": False},
        ],
        "became_eligible": [{"site": "seloger", "id": "3", "actionable": True}],
    }
    assert [event["id"] for event in follows._actionable(payload)] == ["1", "3"]


def test_generated_hermes_script_is_valid_python(tmp_path):
    text = follows._script_content(tmp_path, "paul")
    compile(text, "<generated-follow-script>", "exec")
    assert "watcher.follows" in text
