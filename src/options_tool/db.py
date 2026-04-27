"""SQLAlchemy models and session factory for the options-tool database.

Schema reference: see docs/SCHEMA.md.

Conventions:
- All timestamps stored as UTC, application is responsible for tz-conversion
  at the boundary.
- Enums are stored as plain TEXT with a CHECK constraint so we can introspect
  the DB with any sqlite tool.
- ``stock_positions`` and ``option_positions`` are *snapshots* — wiped and
  re-populated on each IBKR sync. ``transactions`` is an append-only ledger
  used by domain/cost_basis.py.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from options_tool.settings import get_settings

# ---- Base ------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---- Tables ----------------------------------------------------------------


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    ib_account_code: Mapped[str] = mapped_column(String, unique=True)
    alias: Mapped[str | None] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    stock_positions: Mapped[list[StockPosition]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    option_positions: Mapped[list[OptionPosition]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


INTENT_VALUES = ("CORE_HOLD", "INCOME", "TRADE", "WANT_TO_OWN", "WATCH")


class Symbol(Base):
    __tablename__ = "symbols"
    __table_args__ = (
        CheckConstraint(
            f"intent IN {INTENT_VALUES}", name="ck_symbol_intent_valid"
        ),
    )

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    intent: Mapped[str] = mapped_column(String)
    wheel_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    target_buy_price: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Whether to consider non-monthly (weekly / non-3rd-Friday) expiries when
    # running the advisor. Liquidity-driven setting — high-volume names like
    # SPY / NVDA can use weeklies; thin tickers like URA stick to monthlies.
    weekly_ok: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )


class StockPosition(Base):
    __tablename__ = "stock_positions"
    __table_args__ = (UniqueConstraint("account_id", "symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    symbol: Mapped[str] = mapped_column(String)
    qty: Mapped[float] = mapped_column(Float)
    avg_cost: Mapped[float] = mapped_column(Float)
    market_value: Mapped[float | None] = mapped_column(Float)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    account: Mapped[Account] = relationship(back_populates="stock_positions")


RIGHT_VALUES = ("C", "P")


class OptionPosition(Base):
    __tablename__ = "option_positions"
    __table_args__ = (
        UniqueConstraint("account_id", "symbol", "right", "strike", "expiry"),
        CheckConstraint(f"right IN {RIGHT_VALUES}", name="ck_option_position_right"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    symbol: Mapped[str] = mapped_column(String)
    right: Mapped[str] = mapped_column(String)
    strike: Mapped[float] = mapped_column(Float)
    expiry: Mapped[date] = mapped_column(Date)
    qty: Mapped[int] = mapped_column(Integer)  # negative for short
    avg_open_price: Mapped[float | None] = mapped_column(Float)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime)
    current_value: Mapped[float | None] = mapped_column(Float)
    current_delta: Mapped[float | None] = mapped_column(Float)
    current_iv: Mapped[float | None] = mapped_column(Float)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    account: Mapped[Account] = relationship(back_populates="option_positions")


ACTION_VALUES = ("BTO", "STO", "BTC", "STC", "BUY", "SELL", "ASSIGN", "EXPIRE")
ASSET_TYPE_VALUES = ("STOCK", "OPTION")
ORDER_ACTION_VALUES = ("BUY", "SELL")


class OpenOrder(Base):
    """Snapshot of open orders pulled from IBKR via reqAllOpenOrdersAsync.

    Wiped + re-inserted per account on each sync (mirrors position handling).
    Used to:
      - show pending orders in the symbol detail pane
      - dedupe advisor recommendations against already-placed orders
    """

    __tablename__ = "open_orders"
    __table_args__ = (
        CheckConstraint(
            f"asset_type IN {ASSET_TYPE_VALUES}", name="ck_open_order_asset_type"
        ),
        CheckConstraint(
            f"action IN {ORDER_ACTION_VALUES}", name="ck_open_order_action"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    perm_id: Mapped[int | None] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String)
    asset_type: Mapped[str] = mapped_column(String)
    right: Mapped[str | None] = mapped_column(String)
    strike: Mapped[float | None] = mapped_column(Float)
    expiry: Mapped[date | None] = mapped_column(Date)
    action: Mapped[str] = mapped_column(String)
    order_type: Mapped[str] = mapped_column(String)
    qty: Mapped[float] = mapped_column(Float)
    lmt_price: Mapped[float | None] = mapped_column(Float)
    aux_price: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str | None] = mapped_column(String)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(
            f"asset_type IN {ASSET_TYPE_VALUES}", name="ck_tx_asset_type"
        ),
        CheckConstraint(f"action IN {ACTION_VALUES}", name="ck_tx_action"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ib_exec_id: Mapped[str | None] = mapped_column(String, unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    symbol: Mapped[str] = mapped_column(String)
    asset_type: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    right: Mapped[str | None] = mapped_column(String)
    strike: Mapped[float | None] = mapped_column(Float)
    expiry: Mapped[date | None] = mapped_column(Date)
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    executed_at: Mapped[datetime] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)


class Earnings(Base):
    __tablename__ = "earnings"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    earnings_date: Mapped[date] = mapped_column(Date, primary_key=True)
    time_of_day: Mapped[str | None] = mapped_column(String)
    source: Mapped[str | None] = mapped_column(String)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class IVHistory(Base):
    __tablename__ = "iv_history"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    iv_30d: Mapped[float | None] = mapped_column(Float)
    hv_30d: Mapped[float | None] = mapped_column(Float)


class StockPriceHistory(Base):
    """Daily stock closes — backs the Recommendation feedback loop analyzer.

    Populated on-demand when ``analyze-recommendations`` runs: it inspects
    which (symbol, date) pairs it needs for expired recs and pulls any
    missing rows via ``reqHistoricalData``. The table is append-only; each
    (symbol, date) is a unique row that never needs updating.
    """

    __tablename__ = "stock_price_history"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[float] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ChainCache(Base):
    __tablename__ = "chain_cache"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    expiry: Mapped[date] = mapped_column(Date, primary_key=True)
    strike: Mapped[float] = mapped_column(Float, primary_key=True)
    right: Mapped[str] = mapped_column(String, primary_key=True)
    bid: Mapped[float | None] = mapped_column(Float)
    ask: Mapped[float | None] = mapped_column(Float)
    last: Mapped[float | None] = mapped_column(Float)
    delta: Mapped[float | None] = mapped_column(Float)
    gamma: Mapped[float | None] = mapped_column(Float)
    theta: Mapped[float | None] = mapped_column(Float)
    vega: Mapped[float | None] = mapped_column(Float)
    iv: Mapped[float | None] = mapped_column(Float)
    open_interest: Mapped[int | None] = mapped_column(Integer)
    volume: Mapped[int | None] = mapped_column(Integer)
    underlying_price: Mapped[float | None] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    symbol: Mapped[str] = mapped_column(String)
    intent: Mapped[str] = mapped_column(String)
    right: Mapped[str] = mapped_column(String)
    strike: Mapped[float] = mapped_column(Float)
    expiry: Mapped[date] = mapped_column(Date)
    premium: Mapped[float] = mapped_column(Float)
    delta: Mapped[float | None] = mapped_column(Float)
    dte: Mapped[int] = mapped_column(Integer)
    annualized_roc: Mapped[float] = mapped_column(Float)
    rank: Mapped[int] = mapped_column(Integer)
    taken: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str | None] = mapped_column(String)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_key: Mapped[str] = mapped_column(String, unique=True)
    alert_type: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    payload: Mapped[str | None] = mapped_column(Text)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    sent_to_telegram: Mapped[bool] = mapped_column(Boolean, default=False)


# ---- Engine + session ------------------------------------------------------


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
    """Enable WAL mode and FK enforcement for every new SQLite connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(settings.db_url, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), expire_on_commit=False, future=True
        )
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager that commits on success and rolls back on exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(db_path: Path | None = None, *, drop_first: bool = False) -> None:
    """Create all tables. P0 rebuild policy: optional drop_first for resets.

    For columns added after the initial schema we use lightweight in-place
    ``ALTER TABLE`` rather than Alembic — single-user SQLite, P0.
    """
    if db_path is not None:
        # tests / one-off scripts can override the path
        from options_tool import settings as settings_mod

        settings_mod.get_settings.cache_clear()
        global _engine, _SessionLocal
        _engine = None
        _SessionLocal = None
    engine = get_engine()
    if drop_first:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    _apply_inplace_migrations(engine)


def _apply_inplace_migrations(engine: Engine) -> None:
    """Add columns introduced after the initial schema. Idempotent."""
    additions = [
        ("symbols", "hidden", "BOOLEAN NOT NULL DEFAULT 0"),
        ("symbols", "weekly_ok", "BOOLEAN NOT NULL DEFAULT 0"),
    ]
    with engine.begin() as conn:
        for table, col, ddl in additions:
            existing = {
                row[1]
                for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            if col not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
