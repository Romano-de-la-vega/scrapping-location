from __future__ import annotations

from pathlib import Path

import pytest

from watcher.config import ConfigError, config_from_dict, load_config


def test_example_configuration_loads_and_preserves_search_urls() -> None:
    config = load_config(Path(__file__).parents[1] / "config.example.json")
    assert set(config.sites) == {"leboncoin", "seloger", "seventee"}
    assert config.sites["leboncoin"].search_url.endswith("sort=time&order=desc")
    assert "rentIncludingCharges.min=550%23RANGE%23%E2%82%AC" in (
        config.sites["seventee"].search_url
    )
    assert config.scan.max_pages_per_site == 1
    assert config.scan.max_lazy_scrolls == 3
    assert config.scan.navigation_retries == 1
    assert config.diff.suspicious_drop_ratio == 0.5


def test_relative_runtime_paths_are_resolved_next_to_config(tmp_path) -> None:
    config = config_from_dict(
        {
            "timezone": "Europe/Paris",
            "database_path": "state/test.db",
            "sites": {"example": "https://example.test/search?a=1&b=2"},
        },
        base_dir=tmp_path,
    )
    assert config.database_path == tmp_path / "state/test.db"
    assert config.sites["example"].search_url == (
        "https://example.test/search?a=1&b=2"
    )


def test_invalid_ranges_are_rejected() -> None:
    with pytest.raises(ConfigError):
        config_from_dict(
            {
                "criteria": {"price_min": 900, "price_max": 800},
                "sites": {},
            }
        )
