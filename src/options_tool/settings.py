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


# Per-symbol overrides may only touch *filter* knobs, never intent-defining
# (``side`` / ``rank_by``) or project-level hard rules (``exclude_earnings_dte``).
OVERRIDABLE_PRESET_FIELDS: tuple[str, ...] = (
    "delta_min",
    "delta_max",
    "dte_min",
    "dte_max",
    "strike_window_pct",
    "max_strikes_per_side",
    "strike_max_vs_target",
    "top_n",
)


class PresetOverrideError(ValueError):
    """Raised when a per-symbol override dict has unknown / invalid fields."""


def validate_preset_overrides(raw: dict | None) -> dict:
    """Coerce a user-supplied override dict to the right types and reject
    unknown keys / out-of-range values.

    Returns a *new* dict containing only well-typed entries. Empty input → {}.
    Raises ``PresetOverrideError`` with a human-readable message on bad input.
    """
    if not raw:
        return {}
    out: dict = {}
    for key, value in raw.items():
        if key not in OVERRIDABLE_PRESET_FIELDS:
            raise PresetOverrideError(
                f"未知 override 字段 {key!r}（可用：{', '.join(OVERRIDABLE_PRESET_FIELDS)}）"
            )
        if value is None or value == "":
            continue  # treat blank as "no override"
        if key in {"dte_min", "dte_max", "max_strikes_per_side", "top_n"}:
            try:
                coerced: float | int = int(value)
            except (TypeError, ValueError) as exc:
                raise PresetOverrideError(f"{key} 必须是整数（收到 {value!r}）") from exc
            if coerced < 0:
                raise PresetOverrideError(f"{key} 不能为负（收到 {coerced}）")
        else:
            try:
                coerced = float(value)
            except (TypeError, ValueError) as exc:
                raise PresetOverrideError(f"{key} 必须是数字（收到 {value!r}）") from exc
            if key in {"delta_min", "delta_max"} and not (0.0 <= coerced <= 1.0):
                raise PresetOverrideError(f"{key} 必须在 [0, 1]（收到 {coerced}）")
            if key == "strike_window_pct" and not (0.0 < coerced <= 2.0):
                raise PresetOverrideError(
                    f"strike_window_pct 必须在 (0, 2]（收到 {coerced}）"
                )
            if key == "strike_max_vs_target" and not (0.0 < coerced <= 5.0):
                raise PresetOverrideError(
                    f"strike_max_vs_target 必须在 (0, 5]（收到 {coerced}）"
                )
        out[key] = coerced
    # Cross-field sanity checks
    if "delta_min" in out and "delta_max" in out and out["delta_min"] > out["delta_max"]:
        raise PresetOverrideError(
            f"delta_min ({out['delta_min']}) 不能 > delta_max ({out['delta_max']})"
        )
    if "dte_min" in out and "dte_max" in out and out["dte_min"] > out["dte_max"]:
        raise PresetOverrideError(
            f"dte_min ({out['dte_min']}) 不能 > dte_max ({out['dte_max']})"
        )
    return out


def apply_preset_overrides(
    preset: IntentPreset, overrides: dict | None
) -> IntentPreset:
    """Return a new ``IntentPreset`` with ``overrides`` applied on top.

    ``overrides`` is assumed to have already passed ``validate_preset_overrides``
    (the web/CLI entry points do this); unknown keys are ignored defensively.
    Empty / None overrides → preset returned unchanged.
    """
    if not overrides:
        return preset
    patch = {k: v for k, v in overrides.items() if k in OVERRIDABLE_PRESET_FIELDS}
    if not patch:
        return preset
    return preset.model_copy(update=patch)


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
