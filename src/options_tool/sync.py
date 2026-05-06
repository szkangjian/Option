"""Sync IBKR positions to the local SQLite database.

Position tables (``stock_positions``, ``option_positions``) are *snapshots* —
we wipe them per-account and re-insert on each sync. This avoids drift between
IBKR's source-of-truth and our cache when positions close or roll.

The append-only ``transactions`` ledger is updated separately (P1 — pulling
historical fills via reqExecutions). For P0 we only sync open positions.
"""
from __future__ import annotations

from collections.abc import Iterable
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select

from options_tool.db import (
    Account,
    Earnings,
    IVHistory,
    OpenOrder,
    OptionPosition,
    Recommendation,
    StockPosition,
    StockPriceHistory,
    Symbol,
    Transaction,
    session_scope,
)
from options_tool.domain.wheel import (
    AssignmentEvent,
    OptionSnapshot,
    StockSnapshot,
    detect_assignments,
)
from options_tool.finnhub import EarningsRow, fetch_earnings
from options_tool.ibkr import (
    ExecutionRow,
    IVHistoryRow,
    MultiAccountClient,
    OpenOrderRow,
    OptionHolding,
    StockCloseRow,
    StockHolding,
)
from options_tool.settings import AccountConfig

logger = logging.getLogger(__name__)


def _ensure_account(session, ib_account_code: str, alias: str | None) -> Account:
    acct = session.scalar(
        select(Account).where(Account.ib_account_code == ib_account_code)
    )
    if acct is None:
        acct = Account(ib_account_code=ib_account_code, alias=alias, enabled=True)
        session.add(acct)
        session.flush()
    return acct


async def sync_positions(
    configs: Iterable[AccountConfig] | None = None,
    *,
    refresh_earnings: bool = True,
) -> tuple[int, int]:
    """Pull positions + open orders from IBKR, then earnings from Finnhub.

    Earnings is best-effort and enabled by default for CLI/manual sync:
    missing API key or HTTP failure logs and continues. JSON portfolio refresh
    can pass ``refresh_earnings=False`` for a lightweight IB-only snapshot.
    Returns ``(stock_rows, option_rows)`` totals — earnings count is logged
    separately.
    """
    async with MultiAccountClient(configs) as multi:
        stocks, options = await multi.fetch_all_positions()
        orders = await multi.fetch_all_open_orders()

        alias_by_code = {c.cfg.account_code: c.cfg.alias for c in multi.clients}

    stock_n, option_n = _persist_positions(stocks, options, alias_by_code)
    order_n = _persist_open_orders(orders, alias_by_code)

    # Earnings — pull for every tracked symbol in one batch. API portfolio
    # refresh can skip this so it doesn't depend on external HTTP.
    earnings_n = await sync_earnings() if refresh_earnings else 0

    logger.info(
        "Synced %d stocks, %d options, %d open orders, %d earnings rows",
        stock_n, option_n, order_n, earnings_n,
    )
    return stock_n, option_n


async def sync_earnings() -> int:
    """Refresh upcoming earnings dates for all tracked symbols.

    Wipes future earnings (``date >= today``) per symbol then re-inserts
    fresh rows from Finnhub. Past earnings are left alone — they're harmless
    and might be useful for IV-vs-realized analysis later.
    """
    today = date.today()
    with session_scope() as session:
        symbols = list(session.scalars(select(Symbol.symbol)))
    if not symbols:
        return 0

    rows = await fetch_earnings(symbols)
    if not rows:
        return 0

    return _persist_earnings(rows, today)


async def sync_transactions(*, lookback_days: int = 7) -> int:
    """Pull recent fills from IBKR and append to the ``transactions`` ledger.

    IBKR's ``reqExecutions`` API caps history at roughly 7 days, so this is
    a rolling incremental sync — pre-installation fills aren't backfillable
    via the API (use Flex Query exports for that).

    Idempotent: ``ib_exec_id`` is a unique constraint, duplicates are skipped.
    Returns the number of *newly* inserted rows.
    """
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    async with MultiAccountClient() as multi:
        rows = await multi.fetch_all_executions(since=since)
        alias_by_code = {c.cfg.account_code: c.cfg.alias for c in multi.clients}
    if not rows:
        return 0
    return _persist_transactions(rows, alias_by_code)


def _persist_transactions(
    rows: list[ExecutionRow], alias_by_code: dict[str, str]
) -> int:
    inserted = 0
    with session_scope() as session:
        existing_ids = {
            x for (x,) in session.execute(
                select(Transaction.ib_exec_id).where(
                    Transaction.ib_exec_id.in_([r.ib_exec_id for r in rows])
                )
            )
        }
        for r in rows:
            if r.ib_exec_id in existing_ids:
                continue
            acct = _ensure_account(session, r.account_code, alias_by_code.get(r.account_code))
            session.add(
                Transaction(
                    ib_exec_id=r.ib_exec_id,
                    account_id=acct.id,
                    symbol=r.symbol,
                    asset_type=r.asset_type,
                    action=r.action,
                    right=r.right,
                    strike=r.strike,
                    expiry=r.expiry,
                    qty=r.qty,
                    price=r.price,
                    commission=r.commission,
                    executed_at=r.executed_at,
                )
            )
            inserted += 1
    return inserted


async def sync_iv_history(
    symbols: list[str] | None = None, *, lookback: str | None = None
) -> int:
    """Refresh IV/HV history.

    - ``symbols=None`` walks all tracked non-hidden symbols.
    - ``lookback=None`` auto-picks: full backfill (``"1 Y"``) for symbols
      with no rows yet, short refresh (``"10 D"``) otherwise. The 10-day
      window covers a 4-day weekend + buffer for the daily cron.

    Idempotent — primary key (symbol, date) makes re-inserts a merge.
    """
    if symbols is None:
        with session_scope() as session:
            symbols = list(
                session.scalars(
                    select(Symbol.symbol).where(Symbol.hidden == False)  # noqa: E712
                )
            )
    if not symbols:
        return 0

    # Decide lookback per symbol (caller can force one window for everyone).
    with session_scope() as session:
        existing = {
            s for (s,) in session.execute(
                select(IVHistory.symbol).where(IVHistory.symbol.in_(symbols)).distinct()
            )
        }

    inserted = 0
    async with MultiAccountClient() as multi:
        for sym in symbols:
            window = lookback or ("10 D" if sym in existing else "1 Y")
            try:
                rows = await multi.fetch_iv_history(sym, lookback=window)
            except Exception:
                logger.exception("IV history fetch failed for %s", sym)
                continue
            if rows:
                inserted += _persist_iv_history(rows)
    return inserted


async def sync_stock_closes_for_recs() -> tuple[int, int]:
    """Pull daily closes for every (symbol, expiry) where a recommendation's
    expiry has passed but we have no close price cached yet.

    Returns (symbols_fetched, rows_inserted). IB's ``reqHistoricalData`` returns
    a full year at once, so we fetch a single "1 Y" series per symbol and
    insert any date that isn't already cached — much cheaper than per-date
    requests and future-proofs the analyzer (no repeat pull needed for a
    year).
    """
    today = date.today()
    with session_scope() as session:
        recs_needed: set[tuple[str, date]] = set(
            session.execute(
                select(Recommendation.symbol, Recommendation.expiry)
                .where(Recommendation.expiry < today)
            ).all()
        )
        existing: set[tuple[str, date]] = set(
            session.execute(
                select(StockPriceHistory.symbol, StockPriceHistory.date)
            ).all()
        )
    missing_by_symbol: dict[str, list[date]] = {}
    for sym, exp in sorted(recs_needed - existing):
        missing_by_symbol.setdefault(sym, []).append(exp)
    if not missing_by_symbol:
        return 0, 0

    inserted = 0
    symbols_fetched = 0
    async with MultiAccountClient() as multi:
        if not multi.clients:
            logger.warning("No IBKR clients — skipping stock close sync")
            return 0, 0
        for sym in sorted(missing_by_symbol):
            try:
                rows = await multi.fetch_stock_closes(sym, lookback="1 Y")
            except Exception:
                logger.exception("stock close fetch failed for %s", sym)
                continue
            symbols_fetched += 1
            if rows:
                inserted += _persist_stock_closes(rows)
    return symbols_fetched, inserted


def _persist_stock_closes(rows: list[StockCloseRow]) -> int:
    """Idempotent insert by (symbol, date). Never overwrites existing rows."""
    n = 0
    with session_scope() as session:
        for r in rows:
            existing = session.get(
                StockPriceHistory, {"symbol": r.symbol, "date": r.date}
            )
            if existing is not None:
                continue
            session.add(StockPriceHistory(
                symbol=r.symbol, date=r.date, close=r.close,
            ))
            n += 1
    return n


def _persist_iv_history(rows: list[IVHistoryRow]) -> int:
    """Upsert by (symbol, date). SQLite-safe via get-then-update."""
    n = 0
    with session_scope() as session:
        for r in rows:
            existing = session.get(IVHistory, {"symbol": r.symbol, "date": r.date})
            if existing is None:
                session.add(
                    IVHistory(
                        symbol=r.symbol, date=r.date,
                        iv_30d=r.iv_30d, hv_30d=r.hv_30d,
                    )
                )
                n += 1
            else:
                existing.iv_30d = r.iv_30d
                if r.hv_30d is not None:
                    existing.hv_30d = r.hv_30d
                n += 1
    return n


def _persist_earnings(rows: list[EarningsRow], today: date) -> int:
    with session_scope() as session:
        # Wipe future rows for symbols we just fetched (so removed dates
        # actually disappear). Past rows untouched.
        seen_symbols = {r.symbol for r in rows}
        session.execute(
            delete(Earnings)
            .where(Earnings.symbol.in_(seen_symbols))
            .where(Earnings.earnings_date >= today)
        )
        for r in rows:
            session.add(
                Earnings(
                    symbol=r.symbol,
                    earnings_date=r.earnings_date,
                    time_of_day=r.time_of_day,
                    source="finnhub",
                )
            )
    return len(rows)


def _persist_positions(
    stocks: list[StockHolding],
    options: list[OptionHolding],
    alias_by_code: dict[str, str],
) -> tuple[int, int]:
    now = datetime.now(timezone.utc)

    # Capture pre-sync snapshot for wheel detection. Resolve account_id → code
    # eagerly so we can compare against the new snapshot (which uses the same
    # account_code keying) without holding session refs.
    prior_opts, prior_stocks, wheel_symbols = _snapshot_for_wheel()

    with session_scope() as session:
        # Group by account code, then wipe + re-insert per account
        accounts_seen: set[str] = {h.account_code for h in stocks} | {
            h.account_code for h in options
        }
        for code in accounts_seen:
            acct = _ensure_account(session, code, alias_by_code.get(code))
            session.execute(
                delete(StockPosition).where(StockPosition.account_id == acct.id)
            )
            session.execute(
                delete(OptionPosition).where(OptionPosition.account_id == acct.id)
            )

        # Rebuild
        for h in stocks:
            acct = _ensure_account(session, h.account_code, alias_by_code.get(h.account_code))
            session.add(
                StockPosition(
                    account_id=acct.id,
                    symbol=h.symbol,
                    qty=h.qty,
                    avg_cost=h.avg_cost,
                    market_value=h.market_value,
                    last_synced_at=now,
                )
            )
        for h in options:
            acct = _ensure_account(session, h.account_code, alias_by_code.get(h.account_code))
            session.add(
                OptionPosition(
                    account_id=acct.id,
                    symbol=h.symbol,
                    right=h.right,
                    strike=h.strike,
                    expiry=h.expiry,
                    qty=h.qty,
                    avg_open_price=h.avg_open_price,
                    current_value=h.market_value,
                    last_synced_at=now,
                )
            )

        # Auto-register any newly-seen underlying as a tracked symbol with
        # default intent=WATCH. User can re-tag from the web panel.
        seen_tickers = {h.symbol for h in stocks} | {h.symbol for h in options}
        existing = {
            s for (s,) in session.execute(
                select(Symbol.symbol).where(Symbol.symbol.in_(seen_tickers))
            )
        }
        for ticker in seen_tickers - existing:
            session.add(Symbol(symbol=ticker, intent="WATCH", hidden=False))

    # Post-sync: detect assignments + apply wheel intent flips.
    current_opts = [
        OptionSnapshot(
            account_code=h.account_code, symbol=h.symbol, right=h.right,
            strike=h.strike, expiry=h.expiry, qty=h.qty,
        )
        for h in options
    ]
    current_stocks = [
        StockSnapshot(account_code=h.account_code, symbol=h.symbol, qty=h.qty)
        for h in stocks
    ]
    events = detect_assignments(
        prior_opts, prior_stocks, current_opts, current_stocks, wheel_symbols
    )
    if events:
        _apply_wheel_flips(events)

    return len(stocks), len(options)


def _snapshot_for_wheel() -> tuple[list[OptionSnapshot], list[StockSnapshot], set[str]]:
    """Read current DB state into pure dataclasses + load wheel-enabled set.

    Done in its own session so we can drop the SQLAlchemy refs before the
    sync session wipes the tables underneath.
    """
    with session_scope() as session:
        accounts = {a.id: a.ib_account_code for a in session.scalars(select(Account))}
        opts = [
            OptionSnapshot(
                account_code=accounts.get(p.account_id, ""),
                symbol=p.symbol,
                right=p.right,
                strike=p.strike,
                expiry=p.expiry,
                qty=int(p.qty),
            )
            for p in session.scalars(select(OptionPosition))
        ]
        stocks = [
            StockSnapshot(
                account_code=accounts.get(p.account_id, ""),
                symbol=p.symbol,
                qty=p.qty,
            )
            for p in session.scalars(select(StockPosition))
        ]
        wheel_syms = {
            s for (s,) in session.execute(
                select(Symbol.symbol).where(Symbol.wheel_enabled == True)  # noqa: E712
            )
        }
    return opts, stocks, wheel_syms


def _apply_wheel_flips(events: list[AssignmentEvent]) -> None:
    """Update Symbol.intent for each detected assignment. Idempotent: if the
    intent is already at the target value, we still log (so the user knows
    detection fired) but don't double-flip.

    For CC exercises flipping to WANT_TO_OWN, also default target_buy_price
    to the exercised strike when unset — otherwise the scanner silently
    no-ops on the newly-flipped symbol because WANT_TO_OWN requires a
    target. The strike is a sensible default: it's the price the user just
    accepted for the shares, so "willing to rebuy at or below this" is a
    natural start. User can tune it via the Web edit modal or CLI.
    """
    with session_scope() as session:
        for e in events:
            sym = session.get(Symbol, e.symbol)
            if sym is None:
                logger.warning("wheel: %s not in symbols table", e.symbol)
                continue
            old_intent = sym.intent
            sym.intent = e.new_intent

            target_msg = ""
            if (
                e.kind == "cc_exercised"
                and e.new_intent == "WANT_TO_OWN"
                and sym.target_buy_price is None
            ):
                sym.target_buy_price = e.strike
                target_msg = f" · target_buy_price 默认设为 ${e.strike:g}"

            logger.info(
                "🔄 wheel auto-flip: %s %s%g %s × %d → intent %s → %s (%s)%s",
                e.symbol, e.right, e.strike, e.expiry.isoformat(),
                e.contracts, old_intent, e.new_intent, e.kind, target_msg,
            )


def _persist_open_orders(
    orders: list[OpenOrderRow], alias_by_code: dict[str, str]
) -> int:
    """Snapshot open orders. Wipe per-account, re-insert."""
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        accounts_seen = {o.account_code for o in orders}
        # Also wipe accounts that previously had orders but now have none —
        # otherwise stale rows linger after the user cancels everything.
        for acct in session.scalars(select(Account)).all():
            if acct.ib_account_code in accounts_seen or session.scalar(
                select(OpenOrder.id).where(OpenOrder.account_id == acct.id).limit(1)
            ):
                session.execute(
                    delete(OpenOrder).where(OpenOrder.account_id == acct.id)
                )

        for o in orders:
            acct = _ensure_account(session, o.account_code, alias_by_code.get(o.account_code))
            session.add(
                OpenOrder(
                    account_id=acct.id,
                    perm_id=o.perm_id,
                    symbol=o.symbol,
                    asset_type=o.asset_type,
                    right=o.right,
                    strike=o.strike,
                    expiry=o.expiry,
                    action=o.action,
                    order_type=o.order_type,
                    qty=o.qty,
                    lmt_price=o.lmt_price,
                    aux_price=o.aux_price,
                    status=o.status,
                    last_synced_at=now,
                )
            )
    return len(orders)
