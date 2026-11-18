"""Runtime configuration (environment overridable)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _regions(name: str, default: str) -> tuple[str, ...]:
    parts = [p.strip().lower() for p in _env(name, default).split(",") if p.strip()]
    return tuple(dict.fromkeys(parts)) or ("us",)


@dataclass(frozen=True)
class PolicyConfig:
    """Deletion-evidence policy.

    ``min_scans`` counts complete observations (a regional sweep contributes one
    per region). ``min_scan_batches`` prevents a single multi-region sweep from
    masquerading as evidence gathered over time. ``anomaly_extra_scans`` raises
    the bar when a listing is empty or collapses. ``min_span_seconds`` is the
    wall-clock spread required between the oldest and newest accepted scan.
    """

    min_scans: int = 3
    min_regions: int = 2
    min_scan_batches: int = 2
    anomaly_extra_scans: int = 2
    min_span_seconds: int = 0
    min_body_bytes: int = 40

    def describe(self) -> dict:
        return {
            "min_scans": self.min_scans,
            "min_regions": self.min_regions,
            "min_scan_batches": self.min_scan_batches,
            "anomaly_extra_scans": self.anomaly_extra_scans,
            "min_span_seconds": self.min_span_seconds,
            "min_body_bytes": self.min_body_bytes,
        }


@dataclass(frozen=True)
class Config:
    db_path: Path = field(default_factory=lambda: Path(_env("LEDGER_DB", str(ROOT / "data" / "ledger.db"))))
    downstream_db_path: Path = field(
        default_factory=lambda: Path(_env("LEDGER_DOWNSTREAM_DB", str(ROOT / "data" / "downstream.db")))
    )
    web_dir: Path = field(default_factory=lambda: Path(_env("LEDGER_WEB_DIR", str(ROOT / "web"))))
    site_url: str = field(default_factory=lambda: _env("LEDGER_SITE_URL", "http://127.0.0.1:8090"))
    downstream_url: str = field(
        default_factory=lambda: _env("LEDGER_DOWNSTREAM_URL", "http://127.0.0.1:8091")
    )
    admin_token: str = field(default_factory=lambda: _env("LEDGER_ADMIN_TOKEN", "demo-admin-secret"))
    notice_token: str = field(default_factory=lambda: _env("LEDGER_NOTICE_TOKEN", "demo-notice-secret"))
    downstream_token: str = field(
        default_factory=lambda: _env("LEDGER_DOWNSTREAM_TOKEN", "demo-downstream-secret")
    )
    http_timeout: float = float(_env("LEDGER_HTTP_TIMEOUT", "12"))
    page_size: int = _int("LEDGER_PAGE_SIZE", 5)
    body_budget: int = _int("LEDGER_BODY_BUDGET", 24)
    default_regions: tuple[str, ...] = field(
        default_factory=lambda: _regions("LEDGER_REGIONS", "us,eu,apac")
    )
    policy: PolicyConfig = field(
        default_factory=lambda: PolicyConfig(
            min_scans=_int("LEDGER_MIN_SCANS", 3),
            min_regions=_int("LEDGER_MIN_REGIONS", 2),
            min_scan_batches=_int("LEDGER_MIN_SCAN_BATCHES", 2),
            anomaly_extra_scans=_int("LEDGER_ANOMALY_EXTRA_SCANS", 2),
            min_span_seconds=_int("LEDGER_MIN_EVIDENCE_SPAN_SECONDS", 0),
            min_body_bytes=_int("LEDGER_MIN_BODY_BYTES", 40),
        )
    )
    auto_scan_interval: int = _int("LEDGER_AUTO_SCAN_INTERVAL", 86400)
    task_tick_seconds: int = _int("LEDGER_TASK_TICK_SECONDS", 10)
    task_retry_seconds: int = _int("LEDGER_TASK_RETRY_SECONDS", 300)
    outbox_retry_seconds: int = _int("LEDGER_OUTBOX_RETRY_SECONDS", 15)
    app_port: int = _int("LEDGER_APP_PORT", 8080)
    site_port: int = _int("LEDGER_SITE_PORT", 8090)
    downstream_port: int = _int("LEDGER_DOWNSTREAM_PORT", 8091)
    seed_on_start: bool = _env("LEDGER_SEED_ON_START", "1") == "1"

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.downstream_db_path.parent.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    cfg = Config()
    cfg.ensure_dirs()
    return cfg
