"""FastAPI web panel.

Two-pane layout:
  - Left: tracked symbols with intent badges, days-to-earnings, current
    short-option positions count.
  - Right: detail pane filled in by HTMX when a symbol is clicked. Shows
    spot, current open positions on this symbol, and the Opening Advisor's
    Top N candidates.

Backend endpoints:
  GET  /                       full page shell + initial symbol list
  GET  /symbols                symbol list partial (HTMX swap)
  GET  /symbols/new            modal — add a new symbol
  GET  /symbols/{sym}          detail pane partial
  GET  /symbols/{sym}/edit     modal — edit intent / target / wheel / notes
  POST /symbols                create symbol (validates ticker via IBKR)
  PATCH /symbols/{sym}         update symbol fields
  POST /symbols/{sym}/hide     set hidden=true
  POST /symbols/{sym}/unhide   set hidden=false
  POST /symbols/{sym}/advise   trigger advisor (cached chain) → updated detail
  POST /sync-positions         re-sync from IBKR
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from options_tool.advisor import advise_symbol
from options_tool.db import (
    INTENT_VALUES,
    Account,
    ChainCache,
    Earnings,
    IVHistory,
    OpenOrder,
    OptionPosition,
    Recommendation,
    StockPosition,
    Symbol,
    Transaction,
    session_scope,
)
from options_tool.domain.advisor_position import advise_position
from options_tool.domain.alert_detection import ShortPositionSnapshot
from options_tool.domain.cost_basis import TxLeg, adjusted_cost_per_share
from options_tool.domain.expiry_scenarios import scenario_for_short
from options_tool.domain.intents import is_scannable
from options_tool.domain.iv_stats import IVPoint, compute_stats
from options_tool.domain.roll_simulator import RollQuote, simulate_rolls
from options_tool.ibkr import MultiAccountClient
from options_tool.jobs import make_scheduler
from options_tool.settings import (
    OVERRIDABLE_PRESET_FIELDS,
    PresetOverrideError,
    get_settings,
    load_alerts,
    load_intents,
    validate_preset_overrides,
)
from options_tool.sync import sync_positions

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=WEB_DIR / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    scheduler = None
    if settings.scheduler_enabled:
        scheduler = make_scheduler()
        scheduler.start()
        logger.info(
            "scheduler started (chain prefetch every %d min)",
            settings.chain_prefetch_interval_minutes,
        )
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
            logger.info("scheduler stopped")


app = FastAPI(title="options-tool", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


# ---- Helpers ---------------------------------------------------------------


INTENT_COLORS = {
    "INCOME": "bg-emerald-100 text-emerald-800 ring-emerald-200",
    "TRADE": "bg-amber-100 text-amber-800 ring-amber-200",
    "WANT_TO_OWN": "bg-sky-100 text-sky-800 ring-sky-200",
    "CORE_HOLD": "bg-slate-100 text-slate-700 ring-slate-200",
    "WATCH": "bg-zinc-100 text-zinc-700 ring-zinc-200",
}


def _iv_stats_by_symbol(symbols: list[str]) -> dict[str, object]:
    """Bulk-compute IVStats for a set of symbols from iv_history.

    One DB round-trip, then group in Python. Returns ``{symbol: IVStats}``
    omitting symbols with insufficient history (compute_stats returned None).
    """
    if not symbols:
        return {}
    with session_scope() as session:
        rows = session.scalars(
            select(IVHistory)
            .where(IVHistory.symbol.in_(symbols))
            .order_by(IVHistory.symbol, IVHistory.date)
        ).all()
    grouped: dict[str, list[IVPoint]] = {}
    for r in rows:
        if r.iv_30d is None:
            continue
        grouped.setdefault(r.symbol, []).append(
            IVPoint(date=r.date, iv_30d=r.iv_30d, hv_30d=r.hv_30d)
        )
    out: dict[str, object] = {}
    for sym, pts in grouped.items():
        stats = compute_stats(pts)
        if stats is not None:
            out[sym] = stats
    return out


def _sort_tier(intent: str, has_short_opts: bool, has_stock: bool) -> int:
    """Lower = higher in the list."""
    if has_short_opts:
        return 0
    if intent in ("INCOME", "TRADE") and has_stock:
        return 1
    if intent == "WANT_TO_OWN":
        return 2
    if intent == "CORE_HOLD":
        return 3
    return 4  # WATCH (and everything else)


def _build_symbol_rows(*, show_hidden: bool = False) -> list[dict]:
    """Aggregate per-symbol view data.

    Sort: tier (short-opts → INCOME/TRADE → WANT_TO_OWN → CORE_HOLD → WATCH),
    then by holding market value descending, then alphabetical.
    """
    today = date.today()
    with session_scope() as session:
        sym_query = select(Symbol)
        if not show_hidden:
            sym_query = sym_query.where(Symbol.hidden == False)  # noqa: E712
        symbols = session.scalars(sym_query).all()

        stock_qty: dict[str, float] = {}
        stock_mv: dict[str, float] = {}
        for sp in session.scalars(select(StockPosition)).all():
            stock_qty[sp.symbol] = stock_qty.get(sp.symbol, 0.0) + sp.qty
            stock_mv[sp.symbol] = stock_mv.get(sp.symbol, 0.0) + (sp.market_value or 0.0)

        short_opt_count: dict[str, int] = {}
        for op in session.scalars(
            select(OptionPosition).where(OptionPosition.qty < 0)
        ).all():
            short_opt_count[op.symbol] = short_opt_count.get(op.symbol, 0) + abs(op.qty)

        next_earn: dict[str, date] = {}
        for e in session.scalars(
            select(Earnings).where(Earnings.earnings_date >= today)
        ).all():
            cur = next_earn.get(e.symbol)
            if cur is None or e.earnings_date < cur:
                next_earn[e.symbol] = e.earnings_date

        rows = []
        iv_by_sym = _iv_stats_by_symbol([s.symbol for s in symbols])
        for s in symbols:
            earn = next_earn.get(s.symbol)
            dte_earn = (earn - today).days if earn else None
            earn_severity = (
                "high"
                if dte_earn is not None and dte_earn <= 7
                else "med"
                if dte_earn is not None and dte_earn <= 21
                else "low"
            )
            shorts = short_opt_count.get(s.symbol, 0)
            qty = stock_qty.get(s.symbol, 0.0)
            mv = stock_mv.get(s.symbol, 0.0)
            ivs = iv_by_sym.get(s.symbol)
            # WANT_TO_OWN without a target → chain prefetch silently no-ops
            # (advisor bails with an empty result). Surface this in the UI so
            # the user knows why no recommendations are showing up.
            needs_target = (
                s.intent == "WANT_TO_OWN" and s.target_buy_price is None
            )
            rows.append(
                {
                    "symbol": s.symbol,
                    "intent": s.intent,
                    "intent_color": INTENT_COLORS.get(s.intent, "bg-zinc-100"),
                    "scannable": is_scannable(s.intent),
                    "wheel": s.wheel_enabled,
                    "target": s.target_buy_price,
                    "needs_target": needs_target,
                    "qty": qty,
                    "market_value": mv,
                    "short_opts": shorts,
                    "dte_earn": dte_earn,
                    "earn_severity": earn_severity,
                    "hidden": s.hidden,
                    "iv_rank": ivs.iv_rank if ivs else None,
                    "iv_percentile": ivs.iv_percentile if ivs else None,
                    "iv_current": ivs.current_iv if ivs else None,
                    "iv_window": ivs.window_days if ivs else None,
                    "_tier": _sort_tier(s.intent, shorts > 0, qty > 0),
                }
            )
        rows.sort(key=lambda r: (r["_tier"], -r["market_value"], r["symbol"]))
        return rows


def _detail_data(symbol: str) -> dict:
    """Return positions + advisor candidates for a single symbol.

    Stock and option rows are enriched with current marks read from
    ``chain_cache`` *if a recent advisor run populated them*. Otherwise the
    "live" fields render as ``—`` — we never block detail open on a fresh
    IBKR pull (lazy-loading principle).
    """
    today = date.today()
    with session_scope() as session:
        sym = session.get(Symbol, symbol)
        stocks = (
            session.query(StockPosition, Account)
            .join(Account, StockPosition.account_id == Account.id)
            .filter(StockPosition.symbol == symbol)
            .all()
        )
        opts = (
            session.query(OptionPosition, Account)
            .join(Account, OptionPosition.account_id == Account.id)
            .filter(OptionPosition.symbol == symbol)
            .order_by(OptionPosition.expiry, OptionPosition.strike)
            .all()
        )
        future_earnings = list(session.scalars(
            select(Earnings.earnings_date)
            .where(Earnings.symbol == symbol)
            .where(Earnings.earnings_date >= today)
            .order_by(Earnings.earnings_date)
        ))
        chain_rows = session.scalars(
            select(ChainCache).where(ChainCache.symbol == symbol)
        ).all()
        iv_rows = session.scalars(
            select(IVHistory)
            .where(IVHistory.symbol == symbol)
            .order_by(IVHistory.date)
        ).all()
        orders = (
            session.query(OpenOrder, Account)
            .join(Account, OpenOrder.account_id == Account.id)
            .filter(OpenOrder.symbol == symbol)
            .order_by(OpenOrder.expiry.is_(None), OpenOrder.expiry, OpenOrder.strike)
            .all()
        )
        option_txns = session.scalars(
            select(Transaction)
            .where(Transaction.symbol == symbol)
            .where(Transaction.asset_type == "OPTION")
        ).all()

    legs_by_account: dict[int, list[TxLeg]] = {}
    for t in option_txns:
        legs_by_account.setdefault(t.account_id, []).append(
            TxLeg(
                asset_type=t.asset_type,
                action=t.action,
                qty=t.qty,
                price=t.price,
                commission=t.commission or 0.0,
                executed_at=t.executed_at,
            )
        )

    quote_by_key = {(c.expiry, c.strike, c.right): c for c in chain_rows}
    spot, spot_at = _latest_spot(chain_rows)
    # Aggregate stock basis for the symbol: qty-weighted avg cost across all
    # accounts. Only meaningful when the user actually owns shares — CCs on a
    # non-held name can't realize against a basis. Returns None in that case.
    stock_basis: float | None = None
    total_qty = sum(sp.qty for sp, _ in stocks)
    if total_qty > 0:
        stock_basis = (
            sum(sp.qty * sp.avg_cost for sp, _ in stocks) / total_qty
        )
    iv_stats = compute_stats(
        [IVPoint(date=r.date, iv_30d=r.iv_30d, hv_30d=r.hv_30d)
         for r in iv_rows if r.iv_30d is not None]
    )
    alerts_cfg = load_alerts()

    return {
        "symbol": symbol,
        "sym": sym,
        "intent_color": INTENT_COLORS.get(sym.intent, "bg-zinc-100") if sym else "",
        "spot": spot,
        "spot_at": spot_at,
        "stocks": [
            _stock_view(sp, acct, spot, legs_by_account.get(sp.account_id, []))
            for sp, acct in stocks
        ],
        "opts": [
            _option_view(
                op, acct, today, spot,
                quote_by_key.get((op.expiry, op.strike, op.right)),
                future_earnings, alerts_cfg, chain_rows,
                stock_avg_cost=stock_basis,
            )
            for op, acct in opts
        ],
        "orders": [_order_view(o, acct, today) for o, acct in orders],
        "next_earnings": future_earnings[0] if future_earnings else None,
        "iv_stats": iv_stats,
    }


def _opportunity_view(r: Recommendation, roc_threshold: float, ivs=None) -> dict:
    return {
        "id": r.id,
        "symbol": r.symbol,
        "intent": r.intent,
        "intent_color": INTENT_COLORS.get(r.intent, "bg-zinc-100"),
        "right": r.right,
        "strike": r.strike,
        "expiry": r.expiry,
        "dte": r.dte,
        "premium": r.premium,
        "delta": r.delta,
        "annualized_roc": r.annualized_roc,
        "generated_at": r.generated_at,
        "above_threshold": r.annualized_roc >= roc_threshold,
        "taken": r.taken,
        "outcome": r.outcome,
        "iv_rank": ivs.iv_rank if ivs else None,
        "iv_percentile": ivs.iv_percentile if ivs else None,
        "iv_current": ivs.current_iv if ivs else None,
        "iv_window": ivs.window_days if ivs else None,
    }


def _build_dashboard() -> dict:
    """Aggregate "today at a glance" data: positions needing action + top opps.

    Attention list: every live short option whose advice label != HOLD, enriched
    with the same mark/delta the detail page uses (so numbers match). Ordered
    critical → warn → info, then by DTE ascending — nearer expiry first.

    Top opportunities: newest rank-1 Recommendation per symbol from the last
    24h (matches the alert "opportunity" notion but surfaces the whole top
    slate, not just those above the push threshold).
    """
    today = date.today()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    alerts_cfg = load_alerts()

    severity_order = {"critical": 0, "warn": 1, "info": 2}

    with session_scope() as session:
        opts = (
            session.query(OptionPosition, Account)
            .join(Account, OptionPosition.account_id == Account.id)
            .filter(OptionPosition.qty < 0)
            .all()
        )
        chain_rows = session.scalars(select(ChainCache)).all()
        earnings_rows = session.scalars(
            select(Earnings).where(Earnings.earnings_date >= today)
        ).all()
        recs = session.scalars(
            select(Recommendation)
            .where(Recommendation.generated_at >= cutoff)
            .where(Recommendation.rank == 1)
        ).all()

    chain_by_key = {(c.symbol, c.expiry, c.strike, c.right): c for c in chain_rows}
    earnings_by_symbol: dict[str, list[date]] = {}
    for e in earnings_rows:
        earnings_by_symbol.setdefault(e.symbol, []).append(e.earnings_date)
    for sym in earnings_by_symbol:
        earnings_by_symbol[sym].sort()

    attention: list[dict] = []
    for op, acct in opts:
        if op.expiry < today:
            continue
        quote = chain_by_key.get((op.symbol, op.expiry, op.strike, op.right))
        mark = None
        if quote is not None:
            if quote.bid is not None and quote.ask is not None and quote.bid > 0 and quote.ask > 0:
                mark = (quote.bid + quote.ask) / 2.0
            elif quote.last is not None and quote.last > 0:
                mark = quote.last
        snap = ShortPositionSnapshot(
            symbol=op.symbol,
            right=op.right,
            strike=op.strike,
            expiry=op.expiry,
            qty=int(op.qty),
            avg_open_price=op.avg_open_price,
            current_mark=mark,
            current_delta=quote.delta if quote else None,
        )
        advice = advise_position(
            snap, earnings_by_symbol.get(op.symbol, []), today, alerts_cfg
        )
        if advice.label == "HOLD":
            continue
        attention.append(
            {
                "symbol": op.symbol,
                "account": acct.alias or acct.ib_account_code,
                "right": op.right,
                "strike": op.strike,
                "expiry": op.expiry,
                "dte": (op.expiry - today).days,
                "qty": int(op.qty),
                "label": advice.label,
                "severity": advice.severity,
                "reason": advice.reason,
                "mark": mark,
                "delta": quote.delta if quote else None,
            }
        )
    attention.sort(key=lambda r: (severity_order.get(r["severity"], 9), r["dte"]))

    # Top opportunities: keep the newest rank-1 per symbol, rank by annualized_roc.
    latest_by_symbol: dict[str, Recommendation] = {}
    for r in recs:
        cur = latest_by_symbol.get(r.symbol)
        if cur is None or r.generated_at > cur.generated_at:
            latest_by_symbol[r.symbol] = r
    opps = sorted(
        latest_by_symbol.values(), key=lambda r: r.annualized_roc, reverse=True
    )
    top_opps = opps[:8]
    iv_by_sym = _iv_stats_by_symbol([r.symbol for r in top_opps])
    opp_rows = [
        _opportunity_view(r, alerts_cfg.roc_threshold_annual, iv_by_sym.get(r.symbol))
        for r in top_opps
    ]

    return {
        "attention": attention,
        "opportunities": opp_rows,
        "roc_threshold": alerts_cfg.roc_threshold_annual,
    }


def _latest_spot(rows: list[ChainCache]) -> tuple[float | None, datetime | None]:
    best: ChainCache | None = None
    for r in rows:
        if r.underlying_price is None:
            continue
        if best is None or r.fetched_at > best.fetched_at:
            best = r
    return (best.underlying_price, best.fetched_at) if best else (None, None)


def _stock_view(
    sp: StockPosition,
    acct: Account,
    spot: float | None,
    option_legs: list[TxLeg],
) -> dict:
    pnl_pct = None
    if spot is not None and sp.avg_cost:
        pnl_pct = (spot - sp.avg_cost) / sp.avg_cost
    adj_cost = None
    premium_per_share = None
    if option_legs and sp.qty > 0:
        adj_cost = adjusted_cost_per_share(
            raw_avg_cost=sp.avg_cost,
            shares_held=sp.qty,
            option_legs=option_legs,
        )
        premium_per_share = sp.avg_cost - adj_cost
    return {
        "account": acct.alias or acct.ib_account_code,
        "qty": sp.qty,
        "avg_cost": sp.avg_cost,
        "adj_cost": adj_cost,
        "premium_per_share": premium_per_share,
        "current": spot,
        "pnl_pct": pnl_pct,
    }


def _option_view(
    op: OptionPosition,
    acct: Account,
    today: date,
    spot: float | None,
    quote: ChainCache | None,
    future_earnings: list[date],
    alerts_cfg,
    chain_rows: list[ChainCache],
    stock_avg_cost: float | None = None,
) -> dict:
    mark = None
    if quote is not None:
        if quote.bid is not None and quote.ask is not None:
            mark = (quote.bid + quote.ask) / 2
        elif quote.last is not None:
            mark = quote.last
    pnl_pct = None
    if mark is not None and op.avg_open_price:
        # Short positions: profit when current mark < open price (we paid less to close).
        # Long positions: profit when current mark > open price.
        sign = -1 if op.qty < 0 else 1
        pnl_pct = sign * (mark - op.avg_open_price) / op.avg_open_price
    moneyness_pct = None
    if spot is not None and op.strike:
        moneyness_pct = (spot - op.strike) / op.strike  # +ve = spot above strike

    advice = None
    roll_candidates: list = []
    if op.qty < 0:
        snap = ShortPositionSnapshot(
            symbol=op.symbol,
            right=op.right,
            strike=op.strike,
            expiry=op.expiry,
            qty=int(op.qty),
            avg_open_price=op.avg_open_price,
            current_mark=mark,
            current_delta=quote.delta if quote else None,
        )
        advice = advise_position(snap, future_earnings, today, alerts_cfg)
        if advice.label == "ROLL":
            # Buy-to-close at ask is the conservative estimate; fall back to mark.
            close_cost = None
            if quote is not None and quote.ask is not None and quote.ask > 0:
                close_cost = quote.ask
            elif mark is not None and mark > 0:
                close_cost = mark
            roll_quotes = [
                RollQuote(
                    expiry=r.expiry,
                    strike=r.strike,
                    right=r.right,
                    bid=r.bid,
                    ask=r.ask,
                    last=r.last,
                    delta=r.delta,
                )
                for r in chain_rows
            ]
            roll_candidates = simulate_rolls(
                snap, close_cost, roll_quotes, today, alerts_cfg
            )

    assigned_scenario = None
    if op.qty < 0:
        assigned_scenario = scenario_for_short(
            right=op.right,
            strike=op.strike,
            qty=int(op.qty),
            avg_open_price=op.avg_open_price,
            stock_avg_cost=stock_avg_cost if op.right == "C" else None,
        )

    # For short CCs on a name we hold, surface a few below-strike PUTs as
    # "next-cycle CSP" candidates so the user can eyeball the compound
    # path (let CC assign → sell CSP at K'). Filter: expiry strictly after
    # the CC's expiry, strike strictly below CC strike, mid available.
    next_cycle_puts: list[dict] = []
    if op.qty < 0 and op.right == "C" and stock_avg_cost is not None:
        for r in chain_rows:
            if r.right != "P" or r.expiry <= op.expiry or r.strike >= op.strike:
                continue
            if r.bid is None or r.ask is None or r.bid <= 0 or r.ask <= 0:
                continue
            mid = (r.bid + r.ask) / 2
            effective_buy = r.strike - mid
            next_cycle_puts.append({
                "expiry": r.expiry,
                "dte": (r.expiry - today).days,
                "strike": r.strike,
                "mid": mid,
                "delta": r.delta,
                "effective_buy": effective_buy,
                "gap_vs_basis": effective_buy - stock_avg_cost,
            })
        # Sort closest-to-ATM first, then nearest expiry — that's where the
        # comparison to a flat roll is sharpest.
        next_cycle_puts.sort(
            key=lambda p: (-p["strike"], p["expiry"])
        )
        next_cycle_puts = next_cycle_puts[:6]

    return {
        "account": acct.alias or acct.ib_account_code,
        "right": op.right,
        "strike": op.strike,
        "expiry": op.expiry,
        "dte": (op.expiry - today).days,
        "qty": op.qty,
        "avg_open_price": op.avg_open_price,
        "current": mark,
        "pnl_pct": pnl_pct,
        "delta": quote.delta if quote else None,
        "open_interest": quote.open_interest if quote else None,
        "moneyness_pct": moneyness_pct,
        "advice": advice,  # PositionAdvice | None (None for long legs)
        "roll_candidates": roll_candidates,
        "assigned_scenario": assigned_scenario,
        "next_cycle_puts": next_cycle_puts,
    }


def _order_view(o: OpenOrder, acct: Account, today: date) -> dict:
    return {
        "account": acct.alias or acct.ib_account_code,
        "asset_type": o.asset_type,
        "right": o.right,
        "strike": o.strike,
        "expiry": o.expiry,
        "dte": (o.expiry - today).days if o.expiry else None,
        "action": o.action,
        "order_type": o.order_type,
        "qty": o.qty,
        "lmt_price": o.lmt_price,
        "aux_price": o.aux_price,
        "status": o.status,
    }


async def _validate_ticker(ticker: str) -> bool:
    """Round-trip a Stock contract through IBKR. Returns True if a price came back.

    ib_async raises ``ValueError`` when the contract can't be qualified (e.g.
    typo'd ticker — no conId comes back). We swallow it and return False so
    the caller can render a friendly modal error.
    """
    try:
        async with MultiAccountClient() as multi:
            if not multi.clients:
                return False
            spot = await multi.clients[0].fetch_spot(ticker)
        return spot is not None and spot > 0
    except (ValueError, asyncio.TimeoutError):
        return False


# ---- Routes ----------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    rows = _build_symbol_rows()
    dash = _build_dashboard()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "symbols": rows,
            "show_hidden": False,
            "dashboard": dash,
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_partial(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/dashboard.html", {"dashboard": _build_dashboard()}
    )


@app.get("/symbols", response_class=HTMLResponse)
async def symbols_partial(
    request: Request,
    show_hidden: bool = Query(False),
) -> HTMLResponse:
    rows = _build_symbol_rows(show_hidden=show_hidden)
    return templates.TemplateResponse(
        request,
        "partials/symbol_list.html",
        {"symbols": rows, "show_hidden": show_hidden},
    )


@app.get("/symbols/new", response_class=HTMLResponse)
async def symbol_new_modal(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/symbol_form_modal.html",
        {
            "mode": "create",
            "intents": INTENT_VALUES,
            "sym": None,
            "error": None,
            "overridable_fields": OVERRIDABLE_PRESET_FIELDS,
            "intent_defaults": _intent_defaults_for_template(),
        },
    )


@app.get("/symbols/{symbol}/edit", response_class=HTMLResponse)
async def symbol_edit_modal(request: Request, symbol: str) -> HTMLResponse:
    symbol = symbol.upper()
    with session_scope() as session:
        sym = session.get(Symbol, symbol)
        if sym is None:
            raise HTTPException(404, f"unknown symbol {symbol}")
        sym_view = {
            "symbol": sym.symbol,
            "intent": sym.intent,
            "wheel_enabled": sym.wheel_enabled,
            "weekly_ok": sym.weekly_ok,
            "target_buy_price": sym.target_buy_price,
            "notes": sym.notes,
            "hidden": sym.hidden,
            "preset_overrides": sym.preset_overrides or {},
        }
    return templates.TemplateResponse(
        request,
        "partials/symbol_form_modal.html",
        {
            "mode": "edit",
            "intents": INTENT_VALUES,
            "sym": sym_view,
            "error": None,
            "overridable_fields": OVERRIDABLE_PRESET_FIELDS,
            "intent_defaults": _intent_defaults_for_template(),
        },
    )


@app.post("/symbols", response_class=HTMLResponse)
async def symbol_create(
    request: Request,
    symbol: str = Form(...),
    intent: str = Form(...),
    target_buy_price: str = Form(""),
    wheel_enabled: bool = Form(False),
    weekly_ok: bool = Form(False),
    notes: str = Form(""),
) -> HTMLResponse:
    symbol = symbol.strip().upper()
    if not symbol or intent not in INTENT_VALUES:
        return _form_error(request, "create", symbol, intent, "无效的输入")

    raw_form = dict(await request.form())
    try:
        overrides = _parse_overrides_from_form(raw_form)
    except PresetOverrideError as e:
        return _form_error(
            request, "create", symbol, intent, str(e),
            preset_overrides={
                k.removeprefix("override_"): v for k, v in raw_form.items()
                if k.startswith("override_") and v
            },
        )

    with session_scope() as session:
        if session.get(Symbol, symbol):
            return _form_error(
                request, "create", symbol, intent, f"{symbol} 已经在跟踪列表里",
                preset_overrides=overrides,
            )

    if not await _validate_ticker(symbol):
        return _form_error(
            request, "create", symbol, intent, f"IBKR 找不到 {symbol}（拼错了？）",
            preset_overrides=overrides,
        )

    target = _parse_float(target_buy_price)
    if intent == "WANT_TO_OWN" and target is None:
        return _form_error(
            request, "create", symbol, intent, "WANT_TO_OWN 需要 target_buy_price",
            preset_overrides=overrides,
        )

    with session_scope() as session:
        session.add(
            Symbol(
                symbol=symbol,
                intent=intent,
                target_buy_price=target,
                wheel_enabled=wheel_enabled,
                weekly_ok=weekly_ok,
                notes=notes.strip() or None,
                hidden=False,
                preset_overrides=overrides or None,
            )
        )

    return _list_with_close_modal(request, flash=f"已添加 {symbol}")


@app.patch("/symbols/{symbol}", response_class=HTMLResponse)
async def symbol_update(
    request: Request,
    symbol: str,
    intent: str = Form(...),
    target_buy_price: str = Form(""),
    wheel_enabled: bool = Form(False),
    weekly_ok: bool = Form(False),
    notes: str = Form(""),
) -> HTMLResponse:
    symbol = symbol.upper()
    if intent not in INTENT_VALUES:
        return _form_error(request, "edit", symbol, intent, "未知 intent")
    raw_form = dict(await request.form())
    try:
        overrides = _parse_overrides_from_form(raw_form)
    except PresetOverrideError as e:
        return _form_error(
            request, "edit", symbol, intent, str(e),
            preset_overrides={
                k.removeprefix("override_"): v for k, v in raw_form.items()
                if k.startswith("override_") and v
            },
        )
    target = _parse_float(target_buy_price)
    if intent == "WANT_TO_OWN" and target is None:
        return _form_error(
            request, "edit", symbol, intent, "WANT_TO_OWN 需要 target_buy_price",
            preset_overrides=overrides,
        )
    with session_scope() as session:
        sym = session.get(Symbol, symbol)
        if sym is None:
            raise HTTPException(404, f"unknown symbol {symbol}")
        sym.intent = intent
        sym.target_buy_price = target
        sym.wheel_enabled = wheel_enabled
        sym.weekly_ok = weekly_ok
        sym.notes = notes.strip() or None
        sym.preset_overrides = overrides or None

    return _list_with_close_modal(
        request, flash=f"已更新 {symbol}", refresh_symbol=symbol
    )


@app.post("/symbols/{symbol}/hide", response_class=HTMLResponse)
async def symbol_hide(request: Request, symbol: str) -> HTMLResponse:
    return await _toggle_hidden(request, symbol.upper(), True)


@app.post("/symbols/{symbol}/unhide", response_class=HTMLResponse)
async def symbol_unhide(
    request: Request,
    symbol: str,
    show_hidden: bool = Query(True),
) -> HTMLResponse:
    return await _toggle_hidden(request, symbol.upper(), False, show_hidden=show_hidden)


async def _toggle_hidden(
    request: Request, symbol: str, hidden: bool, *, show_hidden: bool = False
) -> HTMLResponse:
    with session_scope() as session:
        sym = session.get(Symbol, symbol)
        if sym is None:
            raise HTTPException(404, f"unknown symbol {symbol}")
        sym.hidden = hidden
    rows = _build_symbol_rows(show_hidden=show_hidden)
    return templates.TemplateResponse(
        request,
        "partials/symbol_list.html",
        {
            "symbols": rows,
            "show_hidden": show_hidden,
            "flash": f"{'已隐藏' if hidden else '已恢复'} {symbol}",
        },
    )


@app.get("/symbols/{symbol}", response_class=HTMLResponse)
async def symbol_detail(request: Request, symbol: str) -> HTMLResponse:
    data = _detail_data(symbol.upper())
    return templates.TemplateResponse(
        request,
        "partials/detail.html",
        {**data, "candidates": None, "loading": False},
    )


@app.post("/symbols/{symbol}/advise", response_class=HTMLResponse)
async def symbol_advise(request: Request, symbol: str) -> HTMLResponse:
    symbol = symbol.upper()
    result = await advise_symbol(symbol)
    # _detail_data reads spot from chain_cache. fetch_and_cache_chain just
    # upserted the sentinel row when the chain came back empty, so the cached
    # value should now match result.spot — but if either path wrote nothing,
    # fall back to result.spot so the UI doesn't regress to "未缓存".
    data = _detail_data(symbol)
    if data.get("spot") is None and result.spot is not None:
        data["spot"] = result.spot
        data["spot_at"] = datetime.now(timezone.utc)
    return templates.TemplateResponse(
        request,
        "partials/detail.html",
        {
            **data,
            "candidates": result.candidates,
            "advise_reason": result.reason,
            "advise_source": result.source,
            "advise_rejection_groups": result.rejection_groups,
            "advise_quotes_total": result.quotes_total,
            "loading": False,
        },
    )


@app.post("/recommendations/{rec_id}/mark", response_class=HTMLResponse)
async def recommendation_mark(
    request: Request,
    rec_id: int,
    action: str = Form(...),
) -> HTMLResponse:
    """Mark a recommendation as taken / dismissed / reset.

    The feedback loop: ``taken=True`` rows will later feed the scoring
    backtest. ``outcome`` stays free-text so we can append win/loss/assigned
    later without migrating.
    """
    if action not in ("take", "dismiss", "reset"):
        raise HTTPException(400, f"unknown action {action!r}")

    with session_scope() as session:
        rec = session.get(Recommendation, rec_id)
        if rec is None:
            raise HTTPException(404, f"recommendation {rec_id} not found")
        if action == "take":
            rec.taken = True
            rec.outcome = "taken"
        elif action == "dismiss":
            rec.taken = False
            rec.outcome = "dismissed"
        else:
            rec.taken = False
            rec.outcome = None
        session.flush()
        ivs = _iv_stats_by_symbol([rec.symbol]).get(rec.symbol)
        view = _opportunity_view(rec, load_alerts().roc_threshold_annual, ivs)

    return templates.TemplateResponse(
        request, "partials/opportunity_row.html", {"o": view}
    )


@app.post("/sync-positions", response_class=HTMLResponse)
async def sync_endpoint(request: Request) -> HTMLResponse:
    n_stock, n_opt = await sync_positions()
    rows = _build_symbol_rows()
    return templates.TemplateResponse(
        request,
        "partials/symbol_list.html",
        {
            "symbols": rows,
            "show_hidden": False,
            "flash": f"已同步 {n_stock} 只股票 · {n_opt} 张期权",
        },
    )


# ---- Form helpers ----------------------------------------------------------


def _parse_float(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _intent_defaults_for_template() -> dict[str, dict]:
    """Map ``{intent: {field: default_value}}`` for the modal placeholders.

    Reads the current ``intents.yaml`` so the form shows what each field
    *would* be if the user leaves the override blank. ``None`` defaults
    (e.g., delta_min for INCOME) become an empty string so Jinja's
    ``placeholder`` ends up blank rather than literal "None".
    """
    out: dict[str, dict] = {}
    for intent, preset in load_intents().items():
        defaults = {}
        for field in OVERRIDABLE_PRESET_FIELDS:
            value = getattr(preset, field, None)
            defaults[field] = "" if value is None else value
        out[intent] = defaults
    return out


def _parse_overrides_from_form(raw: dict[str, str]) -> dict:
    """Pull ``override_*`` form fields out of the POST body, validate, return
    a clean override dict (only set fields included). Raises
    ``PresetOverrideError`` on any invalid input.
    """
    candidate: dict = {}
    for field in OVERRIDABLE_PRESET_FIELDS:
        v = (raw.get(f"override_{field}") or "").strip()
        if v:
            candidate[field] = v
    return validate_preset_overrides(candidate)


def _form_error(
    request: Request,
    mode: str,
    symbol: str,
    intent: str,
    msg: str,
    *,
    preset_overrides: dict | None = None,
) -> HTMLResponse:
    """Re-render the modal with an inline error.

    Form's ``hx-target=#modal-root`` so the modal partial we return goes
    straight back into the modal slot via the main swap. ``preset_overrides``
    preserves whatever the user typed in the override section so they don't
    have to retype on a validation failure.
    """
    sym_view = {
        "symbol": symbol,
        "intent": intent,
        "wheel_enabled": False,
        "target_buy_price": None,
        "notes": None,
        "hidden": False,
        "preset_overrides": preset_overrides or {},
    }
    return templates.TemplateResponse(
        request,
        "partials/symbol_form_modal.html",
        {
            "mode": mode,
            "intents": INTENT_VALUES,
            "sym": sym_view,
            "error": msg,
            "overridable_fields": OVERRIDABLE_PRESET_FIELDS,
            "intent_defaults": _intent_defaults_for_template(),
        },
    )


def _list_with_close_modal(
    request: Request, *, flash: str, refresh_symbol: str | None = None
) -> HTMLResponse:
    """Refresh the list and close the modal.

    Form's ``hx-target=#modal-root`` (so error responses redraw the modal).
    For success we want to close the modal AND refresh the list, so:

    - main swap target = ``#modal-root``; body's primary content is empty,
      which clears the modal (close).
    - an OOB ``<div id="symbol-list" hx-swap-oob="innerHTML">…</div>``
      injects the fresh list into the left pane.

    If ``refresh_symbol`` is set, also fires ``refreshDetail`` so the
    right detail pane re-pulls.
    """
    import json

    rows = _build_symbol_rows()
    list_html = templates.get_template("partials/symbol_list.html").render(
        {
            "request": request,
            "symbols": rows,
            "show_hidden": False,
            "flash": flash,
        }
    )
    body = f'<div id="symbol-list" hx-swap-oob="innerHTML">{list_html}</div>'
    response = HTMLResponse(body)
    if refresh_symbol:
        response.headers["HX-Trigger"] = json.dumps(
            {"refreshDetail": refresh_symbol}
        )
    return response
