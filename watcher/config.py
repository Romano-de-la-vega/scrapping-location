"""Validated JSON configuration for the watcher."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    """Raised when a configuration value is missing or inconsistent."""


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    mode: str = "launch"
    cdp_url: str = "http://127.0.0.1:9224"
    headless: bool = False
    navigation_timeout_ms: int = 15_000
    selector_timeout_ms: int = 10_000
    block_heavy_resources: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"launch", "cdp"}:
            raise ConfigError("browser.mode must be 'launch' or 'cdp'")
        if self.navigation_timeout_ms <= 0 or self.selector_timeout_ms <= 0:
            raise ConfigError("browser timeouts must be positive")
        if self.mode == "cdp" and not str(self.cdp_url).strip():
            raise ConfigError("browser.cdp_url is required in cdp mode")


@dataclass(frozen=True, slots=True)
class CriteriaConfig:
    postal_codes: tuple[str, ...] = ("69006",)
    price_min: int = 550
    price_max: int = 800
    surface_min: float = 30.0
    surface_max: float = 60.0

    def __post_init__(self) -> None:
        postal_codes = tuple(
            code for raw in self.postal_codes if (code := str(raw).strip())
        )
        if not postal_codes:
            raise ConfigError("criteria.postal_codes must not be empty")
        if self.price_min > self.price_max:
            raise ConfigError("criteria.price_min must be <= price_max")
        if self.surface_min > self.surface_max:
            raise ConfigError("criteria.surface_min must be <= surface_max")
        object.__setattr__(self, "postal_codes", postal_codes)
        object.__setattr__(self, "price_min", int(self.price_min))
        object.__setattr__(self, "price_max", int(self.price_max))
        object.__setattr__(self, "surface_min", float(self.surface_min))
        object.__setattr__(self, "surface_max", float(self.surface_max))


@dataclass(frozen=True, slots=True)
class DiffConfig:
    missing_threshold: int = 2
    suspicious_result_ratio: float = 0.25
    suspicious_min_previous_count: int = 2

    @property
    def suspicious_drop_ratio(self) -> float:
        return self.suspicious_result_ratio

    def __post_init__(self) -> None:
        if self.missing_threshold < 1:
            raise ConfigError("diff.missing_threshold must be >= 1")
        if not 0.0 <= self.suspicious_result_ratio <= 1.0:
            raise ConfigError("diff.suspicious_result_ratio must be between 0 and 1")
        if self.suspicious_min_previous_count < 1:
            raise ConfigError("diff.suspicious_min_previous_count must be >= 1")


@dataclass(frozen=True, slots=True)
class SiteConfig:
    name: str
    search_url: str
    enabled: bool = True

    def __post_init__(self) -> None:
        name = str(self.name).strip().lower()
        search_url = str(self.search_url).strip()
        if not name:
            raise ConfigError("site name must not be empty")
        if not search_url:
            raise ConfigError(f"sites.{name}.search_url must not be empty")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "search_url", search_url)


@dataclass(frozen=True, slots=True)
class ScanConfig:
    max_pages_per_site: int = 1
    max_lazy_scrolls: int = 3
    navigation_retries: int = 1

    def __post_init__(self) -> None:
        if self.max_pages_per_site < 1:
            raise ConfigError("scan.max_pages_per_site must be >= 1")
        if self.max_lazy_scrolls < 0:
            raise ConfigError("scan.max_lazy_scrolls must be >= 0")
        if self.navigation_retries not in {0, 1}:
            raise ConfigError("scan.navigation_retries must be 0 or 1")


@dataclass(frozen=True, slots=True)
class AppConfig:
    timezone: str = "Europe/Paris"
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    criteria: CriteriaConfig = field(default_factory=CriteriaConfig)
    diff: DiffConfig = field(default_factory=DiffConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    sites: Mapping[str, SiteConfig] = field(default_factory=dict)
    database_path: Path = Path("data/state.db")
    log_path: Path = Path("data/watcher.log")
    lock_path: Path = Path("data/watcher.lock")
    debug_directory: Path = Path("debug")
    debug_artifacts_on_error: bool = True

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"unknown timezone: {self.timezone}") from exc
    @property
    def search_urls(self) -> dict[str, str]:
        return {name: site.search_url for name, site in self.sites.items()}

    @property
    def debug_dir(self) -> Path:
        return self.debug_directory

    @property
    def max_pages_per_site(self) -> int:
        return self.scan.max_pages_per_site

    @property
    def max_scrolls(self) -> int:
        return self.scan.max_lazy_scrolls

    @property
    def max_retries(self) -> int:
        return self.scan.navigation_retries


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{name} must be a JSON object")
    return value


def _site_configs(data: Mapping[str, Any]) -> dict[str, SiteConfig]:
    """Accept the documented sites form and two harmless legacy shorthands."""

    raw_sites = data.get("sites")
    if raw_sites is None:
        raw_sites = data.get("search_urls", data.get("urls", {}))
    raw_sites = _mapping(raw_sites, "sites")
    sites: dict[str, SiteConfig] = {}
    for raw_name, raw_value in raw_sites.items():
        name = str(raw_name).strip().lower()
        if isinstance(raw_value, str):
            site = SiteConfig(name=name, search_url=raw_value)
        else:
            value = _mapping(raw_value, f"sites.{name}")
            url = value.get("search_url", value.get("url"))
            if url is None:
                raise ConfigError(f"sites.{name}.search_url is required")
            site = SiteConfig(
                name=name,
                search_url=str(url),
                enabled=bool(value.get("enabled", True)),
            )
        sites[site.name] = site
    return sites


def config_from_dict(data: Mapping[str, Any], *, base_dir: Path | None = None) -> AppConfig:
    """Build and validate :class:`AppConfig` from decoded JSON."""

    if not isinstance(data, Mapping):
        raise ConfigError("configuration root must be a JSON object")
    browser_data = _mapping(data.get("browser"), "browser")
    criteria_data = _mapping(data.get("criteria"), "criteria")
    diff_data = _mapping(data.get("diff"), "diff")
    scan_data = _mapping(data.get("scan"), "scan")

    browser = BrowserConfig(
        mode=str(browser_data.get("mode", "launch")),
        cdp_url=str(browser_data.get("cdp_url", "http://127.0.0.1:9224")),
        headless=bool(browser_data.get("headless", False)),
        navigation_timeout_ms=int(browser_data.get("navigation_timeout_ms", 15_000)),
        selector_timeout_ms=int(browser_data.get("selector_timeout_ms", 10_000)),
        block_heavy_resources=bool(browser_data.get("block_heavy_resources", False)),
    )
    criteria = CriteriaConfig(
        postal_codes=tuple(criteria_data.get("postal_codes", ("69006",))),
        price_min=int(criteria_data.get("price_min", 550)),
        price_max=int(criteria_data.get("price_max", 800)),
        surface_min=float(criteria_data.get("surface_min", 30)),
        surface_max=float(criteria_data.get("surface_max", 60)),
    )
    diff = DiffConfig(
        missing_threshold=int(diff_data.get("missing_threshold", 2)),
        suspicious_result_ratio=float(
            diff_data.get(
                "suspicious_drop_ratio",
                diff_data.get("suspicious_result_ratio", 0.25),
            )
        ),
        suspicious_min_previous_count=int(
            diff_data.get("suspicious_min_previous_count", 2)
        ),
    )
    scan = ScanConfig(
        max_pages_per_site=int(
            scan_data.get("max_pages_per_site", data.get("max_pages_per_site", 1))
        ),
        max_lazy_scrolls=int(
            scan_data.get("max_lazy_scrolls", data.get("max_scrolls", 3))
        ),
        navigation_retries=int(
            scan_data.get("navigation_retries", data.get("max_retries", 1))
        ),
    )

    root = Path(base_dir) if base_dir is not None else Path.cwd()

    def relative_path(key: str, default: str) -> Path:
        value = Path(str(data.get(key, default)))
        return value if value.is_absolute() else root / value

    return AppConfig(
        timezone=str(data.get("timezone", "Europe/Paris")),
        browser=browser,
        criteria=criteria,
        diff=diff,
        scan=scan,
        sites=_site_configs(data),
        database_path=relative_path("database_path", "data/state.db"),
        log_path=relative_path("log_path", "data/watcher.log"),
        lock_path=relative_path("lock_path", "data/watcher.lock"),
        debug_directory=relative_path(
            "debug_directory" if "debug_directory" in data else "debug_dir",
            "debug",
        ),
        debug_artifacts_on_error=bool(data.get("debug_artifacts_on_error", True)),
    )


def load_config(path: str | Path = "config.json") -> AppConfig:
    """Load UTF-8 JSON and resolve relative paths beside that file."""

    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {config_path}: {exc}") from exc
    return config_from_dict(data, base_dir=config_path.resolve().parent)
