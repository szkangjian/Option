"""Application settings and config loading.

Settings come from three sources, in order of precedence:
  1. Environment variables (e.g., OPTIONS_TOOL_DB_PATH)
  2. .env file at project root
  3. Defaults defined here

Per-symbol intent presets and account connection details live in YAML files
under ``config/`` because they are richer than flat env vars.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"


class Settings(BaseSettings):
    """Top-level settings, configurable via env or .env."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="OPTIONS_TOOL_",
        extra="ignore",
    )

    db_path: Path = DATA_DIR / "options.db"
    intents_yaml: Path = CONFIG_DIR / "intents.yaml"
    accounts_yaml: Path = CONFIG_DIR / "accounts.yaml"
    alerts_yaml: Path = CONFIG_DIR / "alerts.yaml"

    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # Telegram + Finnhub keys: accept both the prefixed form
    # (``OPTIONS_TOOL_TELEGRAM_BOT_TOKEN``) and the bare conventional name
    # (``TELEGRAM_BOT_TOKEN``) so the .env reads naturally.
    telegram_bot_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "OPTIONS_TOOL_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"
        ),
    )
    telegram_chat_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "OPTIONS_TOOL_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"
        ),
    )

    finnhub_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "OPTIONS_TOOL_FINNHUB_API_KEY", "FINNHUB_API_KEY"
        ),
    )
    # How far ahead to fetch earnings on each sync. 120 days covers ~1 quarter
    # of forward visibility — enough for any DTE we'd ever consider.
    earnings_lookahead_days: int = 120

    # Background scheduler for chain pre-fetch + (later) alerts / IV history.
    # Set to false in tests to skip starting APScheduler at app boot.
    scheduler_enabled: bool = True
    chain_prefetch_interval_minutes: int = 5
    alert_scan_interval_minutes: int = 10

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


# ---- YAML config models ----------------------------------------------------


class AccountConfig(BaseModel):
    """One IB Gateway connection target.

    A user with two accounts that live behind separate Gateway logins runs two
    Gateway instances on different ports; we therefore store host+port+client_id
    *and* the expected account code per entry.
    """

    alias: str
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int
    account_code: str
    enabled: bool = True


class AccountsConfig(BaseModel):
    accounts: list[AccountConfig] = Field(default_factory=list)


class IntentPreset(BaseModel):
    """Filter preset for one intent.

    Fields are deliberately optional because not every intent uses every knob
    (e.g., WANT_TO_OWN uses ``strike_max_vs_target``, INCOME does not).
    """

    side: str  # "CALL" for CC intents, "PUT" for CSP intents
    delta_min: float | None = None
    delta_max: float | None = None
    dte_min: int
    dte_max: int
    rank_by: str = "annualized_roc"  # or "premium_absolute"
    strike_max_vs_target: float | None = None  # for WANT_TO_OWN
    # Strike window around spot for chain pull. INCOME wants wide (catch
    # far-OTM strikes for high-IV names); TRADE wants narrow (ATM zone).
    strike_window_pct: float = 0.25
    max_strikes_per_side: int = 20
    exclude_earnings_dte: bool = True
    top_n: int = 5


class AlertsConfig(BaseModel):
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"
    dedup_window_hours: int = 6

    profit_take_50: bool = True
    profit_take_80: bool = True
    delta_warning: float = 0.40
    delta_critical: float = 0.50
    # Stop-loss: short option's current mark grew to N× original premium received.
    # Tastytrade's canonical defensive rule is 2.0×; raise to 2.5× for noisier names
    # if you hate getting whipsawed out. Set to 0 to disable.
    stop_loss_multiplier: float = 2.0

    iv_spike_pct: float = 30.0
    earnings_conflict_dte: int = 7
    roc_threshold_annual: float = 0.40


# ---- YAML loaders ----------------------------------------------------------


def load_accounts(path: Path | None = None) -> AccountsConfig:
    p = path or get_settings().accounts_yaml
    if not p.exists():
        return AccountsConfig()
    with p.open() as f:
        return AccountsConfig.model_validate(yaml.safe_load(f) or {})


def load_intents(path: Path | None = None) -> dict[str, IntentPreset]:
    p = path or get_settings().intents_yaml
    if not p.exists():
        return {}
    with p.open() as f:
        raw = yaml.safe_load(f) or {}
    return {name: IntentPreset.model_validate(cfg) for name, cfg in raw.items()}


def load_alerts(path: Path | None = None) -> AlertsConfig:
    p = path or get_settings().alerts_yaml
    if not p.exists():
        return AlertsConfig()
    with p.open() as f:
        return AlertsConfig.model_validate(yaml.safe_load(f) or {})
