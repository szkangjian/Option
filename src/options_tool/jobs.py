"""Background jobs.

Currently runs:
  - chain prefetch: every N minutes, walk all scannable non-hidden symbols and
    refresh ``chain_cache`` so the web detail pane opens instantly.
  - alert scan: every M minutes, evaluate all detection rules against the latest
    snapshot in DB (positions + chain cache + earnings + recommendations) and
    dispatch via Telegram.

Both fan out sequentially over symbols — IB Gateway has rate limits and we'd
rather spend 30s once than risk pacing violations.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from options_tool.advisor import (
    fetch_and_cache_chain,
    fetch_and_cache_position_chain,
    upsert_spot,
)
from options_tool.alerts import dispatch_alerts
from options_tool.db import (
    ChainCache,
    Earnings,
    OptionPosition,
    Recommendation,
    Symbol,
    session_scope,
)
from options_tool.ibkr import MultiAccountClient
from options_tool.sync import sync_iv_history, sync_transactions
from options_tool.domain.alert_detection import (
    AlertCandidate,
    OpportunitySnapshot,
    ShortPositionSnapshot,
    detect_delta_risk,
    detect_earnings_conflict,
    detect_opportunity,
    detect_profit_take,
    detect_stop_loss,
)
from options_tool.domain.intents import SCANNABLE_INTENTS
from options_tool.domain.roll_simulator import (
    RollQuote,
    format_roll_suggestion,
    simulate_rolls,
)
from options_tool.settings import get_settings, load_alerts

logger = logging.getLogger(__name__)


def _scannable_symbols() -> list[str]:
    with session_scope() as session:
        rows = session.scalars(
            select(Symbol.symbol)
            .where(Symbol.hidden == False)  # noqa: E712
            .where(Symbol.intent.in_(list(SCANNABLE_INTENTS)))
        ).all()
    return list(rows)


def _watch_symbols() -> list[str]:
    """Non-hidden symbols we only track spot for (WATCH + CORE_HOLD).

    These aren't scanned for CC/CSP candidates, but the detail page still wants
    a current price. We fetch spot only (no chain pull) so it's cheap.
    """
    with session_scope() as session:
        rows = session.scalars(
            select(Symbol.symbol)
            .where(Symbol.hidden == False)  # noqa: E712
            .where(Symbol.intent.in_(["WATCH", "CORE_HOLD"]))
        ).all()
    return list(rows)


# Spot sentinel + upsert helper now live in options_tool.advisor — both the
# scheduler's spot-only watch path and the on-demand chain fetcher write spot
# the same way.


def _position_chain_groups(today: date) -> list[tuple[str, str, int]]:
    """Groups of ``(symbol, right, max_pos_dte)`` for open short options.

    The scheduler uses these groups to refresh chain_cache for the side of
    each open short — the intent-side prefetch only caches one side, so a
    WANT_TO_OWN symbol with a short CALL would otherwise have no CALL quotes
    available to the Roll simulator. Returns at most one entry per
    (symbol, right); the DTE is the furthest-out expiry in the group, so the
    caller can size the fetch window to cover rolls past that date.
    """
    with session_scope() as session:
        rows = session.execute(
            select(OptionPosition.symbol, OptionPosition.right, OptionPosition.expiry)
            .where(OptionPosition.qty < 0)
        ).all()
    max_dte: dict[tuple[str, str], int] = {}
    for sym, right, expiry in rows:
        if expiry < today:
            continue
        dte = (expiry - today).days
        key = (sym, right)
        if dte > max_dte.get(key, -1):
            max_dte[key] = dte
    return [(sym, right, d) for (sym, right), d in sorted(max_dte.items())]


async def prefetch_chains() -> None:
    """Refresh chain cache for scannable symbols, spot-only for WATCH/CORE_HOLD.

    After the intent-side pass, also refresh by-position: for every open short
    option leg, pull the chain for *its* side out to ``max_pos_dte + 60`` so
    the web detail pane's Roll simulator has real candidates instead of the
    intent-side cache it would otherwise see. This covers two cases the
    intent pass misses:
      - symbols with an opposite-side short (e.g. WANT_TO_OWN + short CC)
      - DTE windows further out than the intent preset (e.g. 60-120d rolls)
    """
    scannable = _scannable_symbols()
    watch = _watch_symbols()
    today = date.today()
    position_groups = _position_chain_groups(today)

    if not scannable and not watch and not position_groups:
        logger.debug("prefetch_chains: nothing to refresh")
        return

    if scannable:
        logger.info("prefetch_chains: refreshing %d scannable symbols", len(scannable))
        ok = 0
        for sym in scannable:
            try:
                quotes, _spot, _reason, _source = await fetch_and_cache_chain(sym)
                if quotes:
                    ok += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("prefetch_chains: %s failed", sym)
        logger.info("prefetch_chains: %d/%d chains refreshed", ok, len(scannable))

    if position_groups:
        logger.info(
            "prefetch_chains: refreshing %d position-side groups", len(position_groups)
        )
        ok = 0
        for sym, right, pos_dte in position_groups:
            # Window: today → pos_expiry + 60d. Min 7d so we still catch soon-
            # expiring legs; the dte_max bound covers roll candidates past the
            # current leg.
            try:
                quotes = await fetch_and_cache_position_chain(
                    sym,
                    right,
                    dte_min=min(7, max(1, pos_dte - 7)),
                    dte_max=pos_dte + 60,
                )
                if quotes:
                    ok += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "prefetch_chains: position-side %s/%s failed", sym, right
                )
        logger.info(
            "prefetch_chains: %d/%d position-side groups refreshed",
            ok, len(position_groups),
        )

    if watch:
        logger.info("prefetch_chains: refreshing %d spot-only symbols", len(watch))
        async with MultiAccountClient() as multi:
            if not multi.clients:
                return
            client = multi.clients[0]
            ok = 0
            for sym in watch:
                try:
                    price = await client.fetch_spot(sym)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("prefetch_chains: spot %s failed", sym)
                    continue
                if price is not None:
                    upsert_spot(sym, price)
                    ok += 1
            logger.info("prefetch_chains: %d/%d spots refreshed", ok, len(watch))


def _build_short_snapshots(today: date) -> tuple[
    list[ShortPositionSnapshot],
    dict[str, list[ChainCache]],
    dict[tuple[str, date, float, str], float | None],
]:
    """Join ``option_positions`` (qty < 0) with the latest chain_cache mark.

    Returns:
        snapshots: one per open short leg.
        chain_by_symbol: full chain rows grouped by symbol, ready for the roll
            simulator. Sentinel spot rows (epoch expiry) are filtered out.
        close_cost_by_key: per-leg conservative BTC cost — quote ask if live,
            mark fallback otherwise. Keyed by (symbol, expiry, strike, right).
    """
    with session_scope() as session:
        positions = session.scalars(
            select(OptionPosition).where(OptionPosition.qty < 0)
        ).all()
        chain_rows = session.scalars(select(ChainCache)).all()

    epoch = date(1970, 1, 1)
    chain_by_key = {(c.symbol, c.expiry, c.strike, c.right): c for c in chain_rows}
    chain_by_symbol: dict[str, list[ChainCache]] = {}
    for c in chain_rows:
        if c.expiry == epoch:
            continue  # spot sentinel — useless for roll quotes
        chain_by_symbol.setdefault(c.symbol, []).append(c)

    snaps: list[ShortPositionSnapshot] = []
    close_cost_by_key: dict[tuple[str, date, float, str], float | None] = {}
    for p in positions:
        if p.expiry < today:
            continue
        quote = chain_by_key.get((p.symbol, p.expiry, p.strike, p.right))
        mark: float | None = None
        delta: float | None = None
        close_cost: float | None = None
        if quote is not None:
            if quote.bid is not None and quote.ask is not None and quote.bid > 0 and quote.ask > 0:
                mark = (quote.bid + quote.ask) / 2.0
            elif quote.last is not None and quote.last > 0:
                mark = quote.last
            delta = quote.delta
            # Conservative close estimate: pay ask, fall back to mark.
            if quote.ask is not None and quote.ask > 0:
                close_cost = quote.ask
            elif mark is not None and mark > 0:
                close_cost = mark
        snaps.append(
            ShortPositionSnapshot(
                symbol=p.symbol,
                right=p.right,
                strike=p.strike,
                expiry=p.expiry,
                qty=p.qty,
                avg_open_price=p.avg_open_price,
                current_mark=mark,
                current_delta=delta,
            )
        )
        close_cost_by_key[(p.symbol, p.expiry, p.strike, p.right)] = close_cost
    return snaps, chain_by_symbol, close_cost_by_key


def _enrich_with_roll(
    hit: AlertCandidate,
    pos: ShortPositionSnapshot,
    chain_by_symbol: dict[str, list[ChainCache]],
    close_cost_by_key: dict[tuple[str, date, float, str], float | None],
    today: date,
    cfg,
) -> AlertCandidate:
    """Append a one-line top-1 roll suggestion to a delta-risk alert.

    Returns the original ``hit`` unchanged when no executable roll exists
    (no chain rows, no close cost, or every candidate filtered out by the
    simulator's defense rules).
    """
    chain = chain_by_symbol.get(pos.symbol, [])
    if not chain:
        return hit
    close_cost = close_cost_by_key.get((pos.symbol, pos.expiry, pos.strike, pos.right))
    if close_cost is None:
        return hit
    quotes = [
        RollQuote(
            expiry=c.expiry, strike=c.strike, right=c.right,
            bid=c.bid, ask=c.ask, last=c.last, delta=c.delta,
        )
        for c in chain
    ]
    candidates = simulate_rolls(pos, close_cost, quotes, today, cfg)
    line = format_roll_suggestion(pos, candidates)
    if line is None:
        return hit
    return AlertCandidate(
        alert_key=hit.alert_key,
        alert_type=hit.alert_type,
        symbol=hit.symbol,
        message=hit.message + "\n" + line,
        severity=hit.severity,
    )


def _earnings_by_symbol(today: date) -> dict[str, list[date]]:
    with session_scope() as session:
        rows = session.scalars(
            select(Earnings).where(Earnings.earnings_date >= today)
        ).all()
    out: dict[str, list[date]] = {}
    for r in rows:
        out.setdefault(r.symbol, []).append(r.earnings_date)
    return out


def _recent_top_recommendations(within_minutes: int) -> list[OpportunitySnapshot]:
    """One-per-symbol top-rank recommendation from the last ``within_minutes``.

    The latest prefetch always re-inserts; we just want what surfaced this
    cycle, not the whole historical log. Top rank only — we don't want the
    bot blasting all five candidates at once.
    """
    from datetime import datetime, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
    with session_scope() as session:
        rows = session.scalars(
            select(Recommendation)
            .where(Recommendation.generated_at >= cutoff)
            .where(Recommendation.rank == 1)
        ).all()
        # Dedupe per symbol — keep the latest.
        latest: dict[str, Recommendation] = {}
        for r in rows:
            cur = latest.get(r.symbol)
            if cur is None or r.generated_at > cur.generated_at:
                latest[r.symbol] = r
        return [
            OpportunitySnapshot(
                symbol=r.symbol,
                right=r.right,
                strike=r.strike,
                expiry=r.expiry,
                dte=r.dte,
                premium=r.premium,
                annualized_roc=r.annualized_roc,
                rank=r.rank,
            )
            for r in latest.values()
        ]


async def daily_iv_update() -> None:
    """Append today's IV/HV bar to ``iv_history`` for every tracked symbol.

    Runs once per weekday after the US close. ``sync_iv_history`` auto-picks
    a 10-day window for symbols with prior history (cheap upsert) and a 1-year
    backfill for new ones.
    """
    try:
        n = await sync_iv_history()
        logger.info("daily_iv_update: upserted %d rows", n)
    except Exception:
        logger.exception("daily_iv_update failed")


async def daily_transactions_update() -> None:
    """Capture today's fills before Gateway forgets them.

    IBKR's ``reqExecutions`` API only exposes the *current trading day*'s
    fills — once the day rolls over (or Gateway restarts), they're gone. So
    this job has to run every weekday, ideally after the close while the
    same Gateway session is still up.
    """
    try:
        n = await sync_transactions(lookback_days=1)
        logger.info("daily_transactions_update: inserted %d new fills", n)
    except Exception:
        logger.exception("daily_transactions_update failed")


async def scan_alerts() -> None:
    """Evaluate every detector and dispatch surviving alerts."""
    cfg = load_alerts()
    today = date.today()

    candidates: list[AlertCandidate] = []

    earnings_map = _earnings_by_symbol(today)
    snaps, chain_by_symbol, close_cost_by_key = _build_short_snapshots(today)
    for pos in snaps:
        delta_hit = detect_delta_risk(pos, cfg)
        if delta_hit is not None:
            # Enrich delta-risk pings with the top roll candidate so the
            # phone-side message is actionable, not just "you have a problem".
            delta_hit = _enrich_with_roll(
                delta_hit, pos, chain_by_symbol, close_cost_by_key, today, cfg,
            )
            candidates.append(delta_hit)
        for hit in (
            detect_profit_take(pos, cfg),
            detect_stop_loss(pos, cfg),
            detect_earnings_conflict(pos, earnings_map.get(pos.symbol, []), today, cfg),
        ):
            if hit is not None:
                candidates.append(hit)

    settings = get_settings()
    # "Recent" means roughly the last two prefetch cycles — gives the alert
    # scan some slack but not so much that we replay yesterday's surfaces.
    window = max(settings.chain_prefetch_interval_minutes * 2, 5)
    for opp in _recent_top_recommendations(within_minutes=window):
        hit = detect_opportunity(opp, cfg)
        if hit is not None:
            candidates.append(hit)

    if not candidates:
        logger.debug("scan_alerts: no candidates")
        return

    result = await dispatch_alerts(candidates, cfg)
    logger.info(
        "scan_alerts: %d candidates → sent=%d quiet=%d dedup=%d failed=%d",
        len(candidates), result.sent, result.suppressed_quiet,
        result.suppressed_dedup, result.failed,
    )


def make_scheduler() -> AsyncIOScheduler:
    """Build (but do not start) the AsyncIOScheduler with all jobs registered."""
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone="UTC")
    # Interval triggers default to firing first at start_date + interval, so we
    # don't need to set next_run_time. Setting it to None would *pause* the job.
    scheduler.add_job(
        prefetch_chains,
        trigger="interval",
        minutes=settings.chain_prefetch_interval_minutes,
        id="prefetch_chains",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        scan_alerts,
        trigger="interval",
        minutes=settings.alert_scan_interval_minutes,
        id="scan_alerts",
        coalesce=True,
        max_instances=1,
    )
    # Daily IV history update at 22:00 UTC = 18:00 ET ≈ 90 min after close.
    # Mon-Fri only; weekends have no fresh bar to fetch.
    scheduler.add_job(
        daily_iv_update,
        trigger="cron",
        day_of_week="mon-fri",
        hour=22,
        minute=0,
        id="daily_iv_update",
        coalesce=True,
        max_instances=1,
    )
    # Daily fills capture at 21:00 UTC = 17:00 ET ≈ 1h after close. Must run
    # before Gateway restart, or the day's fills are lost (current-day-only API).
    scheduler.add_job(
        daily_transactions_update,
        trigger="cron",
        day_of_week="mon-fri",
        hour=21,
        minute=0,
        id="daily_transactions_update",
        coalesce=True,
        max_instances=1,
    )
    return scheduler
