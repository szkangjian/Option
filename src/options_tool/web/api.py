"""Read-only JSON API for external integrations (e.g. Dexter agent).

All endpoints are GET-only and never place orders. The HTML/HTMX panel in
``app.py`` continues to be the primary UI; this router exposes the same data
in machine-readable form.

Mounted under ``/api`` from ``app.py`` via ``app.include_router(api_router)``.

Bind to 127.0.0.1 only (the default in ``settings.web_host``). Don't expose
this on a public interface: there is no auth.

Default market-data endpoints return fresh data: they read SQLite cache only
when it is fresh enough for decision support, otherwise they refresh from IB
with a bounded timeout. The ``/api/cached/*`` endpoints are diagnostic-only:
there stale cache returns HTTP 409 with ``fetched_at`` / ``age_seconds``.
Pass ``allow_stale=true`` only for diagnostics.

Endpoints:
  GET  /api/health                         server status
  GET  /api/symbols                        tracked symbols + intent + counts
  GET  /api/portfolio                      cached positions + open orders
  GET  /api/portfolio?refresh=true         sync IB, then return portfolio
  GET  /api/positions/{symbol}             positions for one symbol
  GET  /api/spot/{symbol}                  cached spot; live=true for IB
  GET  /api/option-chain/{symbol}          cached chain; live=true for IB
  GET  /api/live/spot/{symbol}             live IB spot with timeout
  GET  /api/live/option-chain/{symbol}     live IB chain with timeout
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Iterable
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from options_tool.advisor import _persist_chain_cache, upsert_spot
from options_tool.db import (
    Account,
    ChainCache,
    Earnings,
    OpenOrder,
    OptionPosition,
    StockPosition,
    Symbol,
    session_scope,
)
from options_tool.ibkr import ChainFetchResult, MultiAccountClient, OptionQuote
from options_tool.settings import AccountConfig, get_settings, load_api_accounts
from options_tool.sync import sync_positions

logger = logging.getLogger(__name__)

api_router = APIRouter(prefix="/api", tags=["json-api"])

_SPOT_SENTINEL_EXPIRY = date(1970, 1, 1)
_SPOT_SENTINEL_STRIKE = 0.0
_SPOT_SENTINEL_RIGHT = "C"


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    coerced = _as_utc(dt)
    return coerced.isoformat() if coerced else None


def _age_seconds(dt: datetime | None, now: datetime | None = None) -> float | None:
    coerced = _as_utc(dt)
    if coerced is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - coerced).total_seconds())


def _raise_stale_cache(
    *,
    error: str,
    message: str,
    max_age_seconds: float,
    hint: str,
    **details,
) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "error": error,
            "message": message,
            "max_age_seconds": max_age_seconds,
            "hint": hint,
            **details,
        },
    )


def _http_error_code(exc: HTTPException) -> str | None:
    detail = exc.detail
    if isinstance(detail, dict):
        raw = detail.get("error")
        return str(raw) if raw else None
    return None


def _is_stale_cache_error(exc: HTTPException) -> bool:
    return _http_error_code(exc) in {
        "stale_spot_cache",
        "stale_option_chain_cache",
        "stale_portfolio_cache",
    }


def _stock_dict(sp: StockPosition, acct: Account) -> dict:
    return {
        "account": acct.alias or acct.ib_account_code,
        "symbol": sp.symbol,
        "qty": sp.qty,
        "avg_cost": sp.avg_cost,
        "market_value": sp.market_value,
        "last_synced_at": _iso(sp.last_synced_at),
    }


def _option_dict(op: OptionPosition, acct: Account) -> dict:
    return {
        "account": acct.alias or acct.ib_account_code,
        "symbol": op.symbol,
        "right": op.right,
        "strike": op.strike,
        "expiry": op.expiry.isoformat() if op.expiry else None,
        "qty": op.qty,  # negative = short
        "side": "short" if op.qty < 0 else "long",
        "avg_open_price": op.avg_open_price,
        "current_value": op.current_value,
        "current_delta": op.current_delta,
        "current_iv": op.current_iv,
        "opened_at": _iso(op.opened_at),
        "last_synced_at": _iso(op.last_synced_at),
    }


def _order_dict(o: OpenOrder, acct: Account) -> dict:
    return {
        "account": acct.alias or acct.ib_account_code,
        "perm_id": o.perm_id,
        "symbol": o.symbol,
        "asset_type": o.asset_type,
        "right": o.right,
        "strike": o.strike,
        "expiry": o.expiry.isoformat() if o.expiry else None,
        "action": o.action,
        "order_type": o.order_type,
        "qty": o.qty,
        "lmt_price": o.lmt_price,
        "aux_price": o.aux_price,
        "status": o.status,
        "last_synced_at": _iso(o.last_synced_at),
    }


def _normalize_side(
    side: str | None,
    *,
    required: bool = False,
) -> tuple[str | None, str | None]:
    if side is None or not side.strip():
        if required:
            raise HTTPException(status_code=400, detail="side must be CALL or PUT")
        return None, None
    side_u = side.strip().upper()
    if side_u in ("CALL", "C"):
        return "CALL", "C"
    if side_u in ("PUT", "P"):
        return "PUT", "P"
    raise HTTPException(status_code=400, detail="side must be CALL or PUT")


def _enabled_api_accounts() -> list[AccountConfig]:
    accounts = [a for a in load_api_accounts().accounts if a.enabled]
    if not accounts:
        raise HTTPException(status_code=503, detail="No IB accounts configured")
    return accounts


async def _with_ib_timeout[T](
    awaitable: Awaitable[T],
    *,
    timeout_seconds: float,
    label: str,
) -> T:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=f"{label} timed out after {timeout_seconds:g}s",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("%s failed", label)
        raise HTTPException(
            status_code=502,
            detail=f"IB Gateway error: {type(exc).__name__}: {exc}",
        ) from exc


def _latest_sync_at(
    rows: Iterable[StockPosition | OptionPosition | OpenOrder],
) -> datetime | None:
    times = [_as_utc(getattr(r, "last_synced_at", None)) for r in rows]
    present = [t for t in times if t is not None]
    return max(present) if present else None


def _portfolio_payload(
    *,
    refreshed: bool,
    allow_stale: bool = False,
    max_age_seconds: float | None = None,
) -> dict:
    with session_scope() as session:
        stocks = (
            session.query(StockPosition, Account)
            .join(Account, StockPosition.account_id == Account.id)
            .order_by(StockPosition.symbol)
            .all()
        )
        opts = (
            session.query(OptionPosition, Account)
            .join(Account, OptionPosition.account_id == Account.id)
            .order_by(OptionPosition.symbol, OptionPosition.expiry, OptionPosition.strike)
            .all()
        )
        orders = (
            session.query(OpenOrder, Account)
            .join(Account, OpenOrder.account_id == Account.id)
            .order_by(OpenOrder.symbol)
            .all()
        )

    position_rows = [sp for sp, _acct in stocks] + [op for op, _acct in opts]
    all_rows = position_rows + [o for o, _acct in orders]
    last_sync_at = _latest_sync_at(all_rows)
    now = datetime.now(timezone.utc)
    max_age = (
        max_age_seconds
        if max_age_seconds is not None
        else get_settings().api_portfolio_cache_max_age_seconds
    )
    age = _age_seconds(last_sync_at, now)
    stale = age is not None and age > max_age
    if stale and not allow_stale:
        _raise_stale_cache(
            error="stale_portfolio_cache",
            message="Portfolio snapshot is stale",
            max_age_seconds=max_age,
            hint="Use /api/portfolio?refresh=true to sync IB first, or pass allow_stale=true if you only want the cached snapshot.",
            fetched_at=_iso(last_sync_at),
            age_seconds=age,
            stock_positions=len(stocks),
            option_positions=len(opts),
            open_orders=len(orders),
        )
    return {
        "stocks": [_stock_dict(sp, acct) for sp, acct in stocks],
        "options": [_option_dict(op, acct) for op, acct in opts],
        "open_orders": [_order_dict(o, acct) for o, acct in orders],
        "summary": {
            "stock_positions": len(stocks),
            "option_positions": len(opts),
            "open_orders": len(orders),
            "last_sync_at": _iso(last_sync_at),
            "age_seconds": age,
            "max_age_seconds": max_age,
            "stale": stale,
            "refreshed": refreshed,
        },
    }


def _cached_spot_payload(
    symbol: str,
    *,
    raise_on_missing: bool = True,
    allow_stale: bool = False,
    max_age_seconds: float | None = None,
) -> dict | None:
    now = datetime.now(timezone.utc)
    max_age = (
        max_age_seconds
        if max_age_seconds is not None
        else get_settings().api_spot_cache_max_age_seconds
    )
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(ChainCache)
                .where(ChainCache.symbol == symbol)
                .where(ChainCache.underlying_price.is_not(None))
            )
        )
    if not rows:
        if raise_on_missing:
            raise HTTPException(
                status_code=404,
                detail=f"No cached spot for {symbol}; use live=true to query IB",
            )
        return None

    best = max(
        rows,
        key=lambda r: _as_utc(r.fetched_at)
        or datetime.min.replace(tzinfo=timezone.utc),
    )
    age = _age_seconds(best.fetched_at, now)
    stale = age is not None and age > max_age
    if stale and not allow_stale:
        _raise_stale_cache(
            error="stale_spot_cache",
            message=f"Cached spot for {symbol} is stale",
            max_age_seconds=max_age,
            hint="Use live=true or /api/live/spot/{symbol} for a fresh IB quote, or pass allow_stale=true to inspect the cached value.",
            symbol=symbol,
            spot=best.underlying_price,
            fetched_at=_iso(best.fetched_at),
            age_seconds=age,
        )
    return {
        "symbol": symbol,
        "spot": best.underlying_price,
        "source": "cache",
        "cache_hit": True,
        "fetched_at": _iso(best.fetched_at),
        "age_seconds": age,
        "max_age_seconds": max_age,
        "stale": stale,
    }


def _cache_quote_dict(row: ChainCache, now: datetime) -> dict:
    mid = None
    if (
        row.bid is not None
        and row.ask is not None
        and row.bid > 0
        and row.ask > 0
    ):
        mid = (row.bid + row.ask) / 2.0
    elif row.last is not None and row.last > 0:
        mid = row.last
    dte = (row.expiry - date.today()).days
    return {
        "symbol": row.symbol,
        "expiry": row.expiry.isoformat(),
        "dte": dte,
        "strike": row.strike,
        "right": row.right,
        "bid": row.bid,
        "ask": row.ask,
        "mid": mid,
        "last": row.last,
        "delta": row.delta,
        "gamma": row.gamma,
        "theta": row.theta,
        "vega": row.vega,
        "iv": row.iv,
        "open_interest": row.open_interest,
        "volume": row.volume,
        "fetched_at": _iso(row.fetched_at),
        "age_seconds": _age_seconds(row.fetched_at, now),
    }


def _live_quote_dict(q: OptionQuote) -> dict:
    return {
        "symbol": q.symbol,
        "expiry": q.expiry.isoformat(),
        "dte": (q.expiry - date.today()).days,
        "strike": q.strike,
        "right": q.right,
        "bid": q.bid,
        "ask": q.ask,
        "mid": q.mid,
        "last": q.last,
        "delta": q.delta,
        "gamma": q.gamma,
        "theta": q.theta,
        "vega": q.vega,
        "iv": q.iv,
        "open_interest": q.open_interest,
        "volume": q.volume,
    }


def _cache_time_meta(rows: list[ChainCache], now: datetime) -> dict:
    times = [_as_utc(r.fetched_at) for r in rows if r.fetched_at is not None]
    present = [t for t in times if t is not None]
    if not present:
        return {
            "fetched_at": None,
            "age_seconds": None,
            "oldest_fetched_at": None,
            "oldest_age_seconds": None,
        }
    newest = max(present)
    oldest = min(present)
    return {
        "fetched_at": newest.isoformat(),
        "age_seconds": _age_seconds(newest, now),
        "oldest_fetched_at": oldest.isoformat(),
        "oldest_age_seconds": _age_seconds(oldest, now),
    }


def _cached_option_chain_payload(
    symbol: str,
    *,
    side: str | None,
    dte_min: int,
    dte_max: int,
    allow_stale: bool = False,
    max_age_seconds: float | None = None,
) -> dict:
    side_display, right = _normalize_side(side)
    today = date.today()
    now = datetime.now(timezone.utc)
    max_age = (
        max_age_seconds
        if max_age_seconds is not None
        else get_settings().api_option_chain_cache_max_age_seconds
    )

    with session_scope() as session:
        rows = list(
            session.scalars(
                select(ChainCache)
                .where(ChainCache.symbol == symbol)
                .where(ChainCache.expiry != _SPOT_SENTINEL_EXPIRY)
            )
        )

    filtered: list[ChainCache] = []
    for row in rows:
        if right is not None and row.right != right:
            continue
        dte = (row.expiry - today).days
        if dte_min <= dte <= dte_max:
            filtered.append(row)
    filtered.sort(key=lambda r: (r.expiry, r.right, r.strike))

    spot_payload = _cached_spot_payload(
        symbol,
        raise_on_missing=False,
        allow_stale=True,
    )
    meta = _cache_time_meta(filtered, now)
    reason = None
    if not filtered:
        reason = "No cached option quotes for this symbol/side/DTE window"
    spot_stale = (
        spot_payload is not None
        and spot_payload["age_seconds"] is not None
        and spot_payload["age_seconds"] > get_settings().api_spot_cache_max_age_seconds
    )
    stale_rows = [
        row for row in filtered
        if (age := _age_seconds(row.fetched_at, now)) is not None
        and age > max_age
    ]
    stale = bool(stale_rows) or (bool(filtered) and spot_stale)
    if stale and not allow_stale:
        _raise_stale_cache(
            error="stale_option_chain_cache",
            message="Cached option chain contains stale quotes",
            max_age_seconds=max_age,
            hint="Use live=true or /api/live/option-chain/{symbol} for a fresh IB chain, or pass allow_stale=true to inspect cached rows with per-quote ages.",
            symbol=symbol,
            side=side_display or "ALL",
            dte_window=[dte_min, dte_max],
            count=len(filtered),
            stale_count=len(stale_rows),
            spot_stale=spot_stale,
            spot_fetched_at=spot_payload["fetched_at"] if spot_payload else None,
            spot_age_seconds=spot_payload["age_seconds"] if spot_payload else None,
            fetched_at=meta["fetched_at"],
            age_seconds=meta["age_seconds"],
            oldest_fetched_at=meta["oldest_fetched_at"],
            oldest_age_seconds=meta["oldest_age_seconds"],
        )

    return {
        "symbol": symbol,
        "side": side_display or "ALL",
        "spot": spot_payload["spot"] if spot_payload else None,
        "spot_fetched_at": spot_payload["fetched_at"] if spot_payload else None,
        "spot_age_seconds": spot_payload["age_seconds"] if spot_payload else None,
        "dte_window": [dte_min, dte_max],
        "quotes": [_cache_quote_dict(row, now) for row in filtered],
        "count": len(filtered),
        "reason": reason,
        "source": "cache",
        "cache_hit": bool(filtered),
        "max_age_seconds": max_age,
        "stale": stale,
        "stale_count": len(stale_rows),
        "spot_stale": spot_stale,
        **meta,
    }


async def _fetch_live_spot(symbol: str) -> float | None:
    accounts = _enabled_api_accounts()
    async with MultiAccountClient([accounts[0]]) as multi:
        if not multi.clients:
            raise HTTPException(status_code=503, detail="No IB accounts configured")
        return await multi.clients[0].fetch_spot(symbol)


async def _live_spot_payload(symbol: str) -> dict:
    settings = get_settings()
    spot = await _with_ib_timeout(
        _fetch_live_spot(symbol),
        timeout_seconds=settings.api_spot_timeout_seconds,
        label=f"IB spot {symbol}",
    )
    if spot is None or spot <= 0:
        raise HTTPException(status_code=404, detail=f"No spot price for {symbol}")
    upsert_spot(symbol, spot)
    return {
        "symbol": symbol,
        "spot": spot,
        "source": "ibkr",
        "cache_hit": False,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


async def _fetch_live_option_chain(
    symbol: str,
    *,
    side: str,
    dte_min: int,
    dte_max: int,
    strike_window_pct: float,
    max_strikes_per_side: int,
) -> ChainFetchResult:
    accounts = _enabled_api_accounts()
    async with MultiAccountClient([accounts[0]]) as multi:
        if not multi.clients:
            raise HTTPException(status_code=503, detail="No IB accounts configured")
        return await multi.clients[0].fetch_option_chain(
            symbol,
            side=side,
            dte_min=dte_min,
            dte_max=dte_max,
            strike_window_pct=strike_window_pct,
            max_strikes_per_side=max_strikes_per_side,
        )


async def _live_option_chain_payload(
    symbol: str,
    *,
    side: str,
    dte_min: int,
    dte_max: int,
    strike_window_pct: float,
    max_strikes_per_side: int,
) -> dict:
    side_display, _right = _normalize_side(side, required=True)
    assert side_display is not None
    settings = get_settings()
    result = await _with_ib_timeout(
        _fetch_live_option_chain(
            symbol,
            side=side_display,
            dte_min=dte_min,
            dte_max=dte_max,
            strike_window_pct=strike_window_pct,
            max_strikes_per_side=max_strikes_per_side,
        ),
        timeout_seconds=settings.api_option_chain_timeout_seconds,
        label=f"IB option chain {symbol}",
    )

    if result.quotes:
        _persist_chain_cache(result.quotes)
    elif result.spot is not None:
        upsert_spot(symbol, result.spot)

    return {
        "symbol": symbol,
        "side": side_display,
        "spot": result.spot,
        "dte_window": [dte_min, dte_max],
        "quotes": [_live_quote_dict(q) for q in result.quotes],
        "count": len(result.quotes),
        "reason": result.reason,
        "source": result.source,
        "cache_hit": False,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


async def _live_option_chain_request_payload(
    symbol: str,
    *,
    side: str | None,
    dte_min: int,
    dte_max: int,
    strike_window_pct: float,
    max_strikes_per_side: int,
) -> dict:
    """Fetch the live chain requested by the public endpoint.

    If ``side`` is omitted, fetch both sides. This is slower, but it keeps the
    bridge responsible for freshness instead of forcing agent callers to know
    which side was missing from cache.
    """
    if side is not None:
        return await _live_option_chain_payload(
            symbol,
            side=side,
            dte_min=dte_min,
            dte_max=dte_max,
            strike_window_pct=strike_window_pct,
            max_strikes_per_side=max_strikes_per_side,
        )

    call_payload = await _live_option_chain_payload(
        symbol,
        side="CALL",
        dte_min=dte_min,
        dte_max=dte_max,
        strike_window_pct=strike_window_pct,
        max_strikes_per_side=max_strikes_per_side,
    )
    put_payload = await _live_option_chain_payload(
        symbol,
        side="PUT",
        dte_min=dte_min,
        dte_max=dte_max,
        strike_window_pct=strike_window_pct,
        max_strikes_per_side=max_strikes_per_side,
    )
    quotes = call_payload["quotes"] + put_payload["quotes"]
    return {
        "symbol": symbol,
        "side": "ALL",
        "spot": call_payload.get("spot") or put_payload.get("spot"),
        "dte_window": [dte_min, dte_max],
        "quotes": quotes,
        "count": len(quotes),
        "reason": None if quotes else "No live option quotes returned",
        "source": "ibkr",
        "cache_hit": False,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@api_router.get("/health")
async def health() -> dict:
    s = get_settings()
    db_path: Path | None = None
    last_sync_at: datetime | None = None
    with session_scope() as session:
        latest = session.scalars(
            select(StockPosition.last_synced_at).order_by(
                StockPosition.last_synced_at.desc()
            ).limit(1)
        ).first()
        if latest:
            last_sync_at = latest
    try:
        db_path = Path(s.db_path)
    except Exception:
        pass
    return {
        "status": "ok",
        "service": "options-tool",
        "db_path": str(db_path) if db_path else None,
        "last_sync_at": _iso(last_sync_at),
        "last_sync_age_seconds": _age_seconds(last_sync_at),
        "scheduler_enabled": s.scheduler_enabled,
        "api_client_id_offset": s.api_client_id_offset,
        "cache_max_age_seconds": {
            "spot": s.api_spot_cache_max_age_seconds,
            "option_chain": s.api_option_chain_cache_max_age_seconds,
            "portfolio": s.api_portfolio_cache_max_age_seconds,
        },
        "now": datetime.now(timezone.utc).isoformat(),
    }


@api_router.get("/symbols")
async def list_symbols(include_hidden: bool = False) -> dict:
    """Return tracked symbols with intent + position summary (DB read)."""
    today = date.today()
    with session_scope() as session:
        sym_query = select(Symbol)
        if not include_hidden:
            sym_query = sym_query.where(Symbol.hidden == False)  # noqa: E712
        symbols = session.scalars(sym_query).all()

        stock_qty: dict[str, float] = {}
        for sp in session.scalars(select(StockPosition)).all():
            stock_qty[sp.symbol] = stock_qty.get(sp.symbol, 0.0) + sp.qty

        short_opt_count: dict[str, int] = {}
        long_opt_count: dict[str, int] = {}
        for op in session.scalars(select(OptionPosition)).all():
            if op.qty < 0:
                short_opt_count[op.symbol] = short_opt_count.get(op.symbol, 0) + abs(op.qty)
            else:
                long_opt_count[op.symbol] = long_opt_count.get(op.symbol, 0) + op.qty

        next_earn: dict[str, date] = {}
        for e in session.scalars(
            select(Earnings).where(Earnings.earnings_date >= today)
        ).all():
            cur = next_earn.get(e.symbol)
            if cur is None or e.earnings_date < cur:
                next_earn[e.symbol] = e.earnings_date

        out = []
        for sym in symbols:
            out.append({
                "symbol": sym.symbol,
                "intent": sym.intent,
                "wheel_enabled": sym.wheel_enabled,
                "target_buy_price": sym.target_buy_price,
                "stock_qty": stock_qty.get(sym.symbol, 0.0),
                "short_option_contracts": short_opt_count.get(sym.symbol, 0),
                "long_option_contracts": long_opt_count.get(sym.symbol, 0),
                "next_earnings": next_earn[sym.symbol].isoformat() if sym.symbol in next_earn else None,
                "notes": sym.notes,
                "hidden": sym.hidden,
            })
    return {"count": len(out), "symbols": out}


@api_router.get("/portfolio")
async def portfolio(
    refresh: bool = Query(False),
    allow_stale: bool = Query(False),
    max_age_seconds: float | None = Query(None, gt=0.0, le=86400.0),
) -> dict:
    """Return positions + open orders.

    Default is fresh-or-refresh: a fresh SQLite snapshot is returned directly;
    stale cache triggers a bounded IB sync using API-only client IDs.
    """
    if refresh:
        settings = get_settings()
        await _with_ib_timeout(
            sync_positions(
                configs=_enabled_api_accounts(),
                refresh_earnings=False,
            ),
            timeout_seconds=settings.api_portfolio_refresh_timeout_seconds,
            label="IB portfolio refresh",
        )
        return _portfolio_payload(
            refreshed=True,
            allow_stale=allow_stale,
            max_age_seconds=max_age_seconds,
        )

    try:
        return _portfolio_payload(
            refreshed=False,
            allow_stale=allow_stale,
            max_age_seconds=max_age_seconds,
        )
    except HTTPException as exc:
        if allow_stale or not _is_stale_cache_error(exc):
            raise

    settings = get_settings()
    await _with_ib_timeout(
        sync_positions(
            configs=_enabled_api_accounts(),
            refresh_earnings=False,
        ),
        timeout_seconds=settings.api_portfolio_refresh_timeout_seconds,
        label="IB portfolio refresh",
    )
    return _portfolio_payload(
        refreshed=True,
        allow_stale=False,
        max_age_seconds=max_age_seconds,
    )


@api_router.get("/positions/{symbol}")
async def positions_for_symbol(symbol: str) -> dict:
    """Return stock + option positions + open orders for one symbol."""
    sym = symbol.upper()
    today = date.today()
    with session_scope() as session:
        stocks = (
            session.query(StockPosition, Account)
            .join(Account, StockPosition.account_id == Account.id)
            .filter(StockPosition.symbol == sym)
            .all()
        )
        opts = (
            session.query(OptionPosition, Account)
            .join(Account, OptionPosition.account_id == Account.id)
            .filter(OptionPosition.symbol == sym)
            .order_by(OptionPosition.expiry, OptionPosition.strike)
            .all()
        )
        orders = (
            session.query(OpenOrder, Account)
            .join(Account, OpenOrder.account_id == Account.id)
            .filter(OpenOrder.symbol == sym)
            .all()
        )
        sym_row = session.get(Symbol, sym)
        future_earnings = list(session.scalars(
            select(Earnings.earnings_date)
            .where(Earnings.symbol == sym)
            .where(Earnings.earnings_date >= today)
            .order_by(Earnings.earnings_date)
        ))

    total_qty = sum(sp.qty for sp, _ in stocks)
    avg_cost = (
        sum(sp.qty * sp.avg_cost for sp, _ in stocks) / total_qty if total_qty > 0 else None
    )
    last_sync_at = _latest_sync_at(
        [sp for sp, _acct in stocks]
        + [op for op, _acct in opts]
        + [o for o, _acct in orders]
    )

    return {
        "symbol": sym,
        "intent": sym_row.intent if sym_row else None,
        "wheel_enabled": sym_row.wheel_enabled if sym_row else None,
        "target_buy_price": sym_row.target_buy_price if sym_row else None,
        "notes": sym_row.notes if sym_row else None,
        "aggregate_stock_qty": total_qty,
        "aggregate_avg_cost": avg_cost,
        "last_sync_at": _iso(last_sync_at),
        "age_seconds": _age_seconds(last_sync_at),
        "stocks": [_stock_dict(sp, acct) for sp, acct in stocks],
        "options": [_option_dict(op, acct) for op, acct in opts],
        "open_orders": [_order_dict(o, acct) for o, acct in orders],
        "next_earnings": future_earnings[0].isoformat() if future_earnings else None,
        "all_future_earnings": [d.isoformat() for d in future_earnings],
    }


@api_router.get("/cached/spot/{symbol}")
async def cached_spot_price(
    symbol: str,
    allow_stale: bool = Query(False),
    max_age_seconds: float | None = Query(None, gt=0.0, le=86400.0),
) -> dict:
    """Return the latest cached spot price from SQLite."""
    return _cached_spot_payload(
        symbol.upper(),
        allow_stale=allow_stale,
        max_age_seconds=max_age_seconds,
    )


@api_router.get("/live/spot/{symbol}")
async def live_spot_price(symbol: str) -> dict:
    """Fetch live spot price from IB Gateway with a hard timeout."""
    return await _live_spot_payload(symbol.upper())


@api_router.get("/spot/{symbol}")
async def spot_price(
    symbol: str,
    live: bool = Query(False),
    allow_stale: bool = Query(False),
    max_age_seconds: float | None = Query(None, gt=0.0, le=86400.0),
) -> dict:
    """Return fresh spot, auto-refreshing stale/missing cache via IB."""
    sym = symbol.upper()
    if live:
        return await _live_spot_payload(sym)
    try:
        return _cached_spot_payload(
            sym,
            allow_stale=allow_stale,
            max_age_seconds=max_age_seconds,
        )
    except HTTPException as exc:
        if allow_stale:
            raise
        if exc.status_code == 404 or _is_stale_cache_error(exc):
            return await _live_spot_payload(sym)
        raise


@api_router.get("/cached/option-chain/{symbol}")
async def cached_option_chain(
    symbol: str,
    side: str | None = Query(None, description='"CALL", "PUT", or omitted for both'),
    dte_min: int = Query(20, ge=0, le=730),
    dte_max: int = Query(60, ge=1, le=730),
    allow_stale: bool = Query(False),
    max_age_seconds: float | None = Query(None, gt=0.0, le=86400.0),
) -> dict:
    """Return cached option quotes from SQLite."""
    if dte_min > dte_max:
        raise HTTPException(status_code=400, detail="dte_min must be <= dte_max")
    return _cached_option_chain_payload(
        symbol.upper(),
        side=side,
        dte_min=dte_min,
        dte_max=dte_max,
        allow_stale=allow_stale,
        max_age_seconds=max_age_seconds,
    )


@api_router.get("/live/option-chain/{symbol}")
async def live_option_chain(
    symbol: str,
    side: str = Query(..., description='"CALL" or "PUT"'),
    dte_min: int = Query(20, ge=0, le=730),
    dte_max: int = Query(60, ge=1, le=730),
    strike_window_pct: float = Query(0.20, gt=0.0, le=1.0),
    max_strikes_per_side: int = Query(15, ge=1, le=50),
) -> dict:
    """Fetch live option chain quotes from IB Gateway with a hard timeout."""
    if dte_min > dte_max:
        raise HTTPException(status_code=400, detail="dte_min must be <= dte_max")
    return await _live_option_chain_payload(
        symbol.upper(),
        side=side,
        dte_min=dte_min,
        dte_max=dte_max,
        strike_window_pct=strike_window_pct,
        max_strikes_per_side=max_strikes_per_side,
    )


@api_router.get("/option-chain/{symbol}")
async def option_chain(
    symbol: str,
    side: str | None = Query(None, description='"CALL", "PUT", or omitted for both'),
    dte_min: int = Query(20, ge=0, le=730),
    dte_max: int = Query(60, ge=1, le=730),
    strike_window_pct: float = Query(0.20, gt=0.0, le=1.0),
    max_strikes_per_side: int = Query(15, ge=1, le=50),
    live: bool = Query(False),
    allow_stale: bool = Query(False),
    max_age_seconds: float | None = Query(None, gt=0.0, le=86400.0),
) -> dict:
    """Return fresh chain, auto-refreshing stale/missing cache via IB."""
    if dte_min > dte_max:
        raise HTTPException(status_code=400, detail="dte_min must be <= dte_max")
    sym = symbol.upper()
    if live:
        return await _live_option_chain_request_payload(
            sym,
            side=side,
            dte_min=dte_min,
            dte_max=dte_max,
            strike_window_pct=strike_window_pct,
            max_strikes_per_side=max_strikes_per_side,
        )
    try:
        payload = _cached_option_chain_payload(
            sym,
            side=side,
            dte_min=dte_min,
            dte_max=dte_max,
            allow_stale=allow_stale,
            max_age_seconds=max_age_seconds,
        )
    except HTTPException as exc:
        if allow_stale:
            raise
        if _is_stale_cache_error(exc):
            return await _live_option_chain_request_payload(
                sym,
                side=side,
                dte_min=dte_min,
                dte_max=dte_max,
                strike_window_pct=strike_window_pct,
                max_strikes_per_side=max_strikes_per_side,
            )
        raise
    if not allow_stale and payload["count"] == 0:
        return await _live_option_chain_request_payload(
            sym,
            side=side,
            dte_min=dte_min,
            dte_max=dte_max,
            strike_window_pct=strike_window_pct,
            max_strikes_per_side=max_strikes_per_side,
        )
    return payload
