"""Composition layer: IBKR + intent presets + advisor + persistence.

``advise_symbol`` is the entry point used by the CLI and the web panel. It:
  1. Looks up the symbol's intent (or accepts an override).
  2. Resolves the preset from intents.yaml.
  3. Pulls a filtered option chain from IBKR.
  4. Pulls earnings dates from the local DB (if any).
  5. Runs the advisor.
  6. Persists every Top-N row to the ``recommendations`` table.
  7. Returns the candidates for display.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy import select

from options_tool.db import (
    ChainCache,
    Earnings,
    OpenOrder,
    OptionPosition,
    Recommendation,
    Symbol,
    session_scope,
)
from options_tool.domain.advisor_open import Candidate, rank_chain
from options_tool.domain.intents import (
    SCANNABLE_INTENTS,
    FilterableQuote,
    is_scannable,
)
from options_tool.ibkr import MultiAccountClient, OptionQuote
from options_tool.settings import IntentPreset, load_accounts, load_intents

logger = logging.getLogger(__name__)


def _quote_to_filterable(q: OptionQuote) -> FilterableQuote:
    return FilterableQuote(
        symbol=q.symbol,
        expiry=q.expiry,
        strike=q.strike,
        right=q.right,
        bid=q.bid,
        ask=q.ask,
        delta=q.delta,
        last=q.last,
        open_interest=q.open_interest,
    )


def _load_symbol(symbol: str) -> Symbol | None:
    with session_scope() as session:
        return session.get(Symbol, symbol)


def _load_earnings(symbol: str) -> list[date]:
    with session_scope() as session:
        rows = session.scalars(
            select(Earnings.earnings_date)
            .where(Earnings.symbol == symbol)
            .where(Earnings.earnings_date >= date.today())
        ).all()
    return list(rows)


def _load_short_position_keys(symbol: str) -> set[tuple[str, float, date]]:
    """Keys for already-open short option positions on this symbol.

    Used to deduplicate advisor candidates so we don't recommend a contract
    the user has already sold.
    """
    with session_scope() as session:
        rows = session.execute(
            select(OptionPosition.right, OptionPosition.strike, OptionPosition.expiry)
            .where(OptionPosition.symbol == symbol)
            .where(OptionPosition.qty < 0)
        ).all()
    return {(r, s, e) for r, s, e in rows}


def _load_pending_option_order_keys(symbol: str) -> set[tuple[str, float, date]]:
    """Keys for option contracts on this symbol with a pending order.

    Any pending order — open or close — on the same (right, strike, expiry)
    should suppress a recommendation. The contract is in flight; recommending
    it again is noise.
    """
    with session_scope() as session:
        rows = session.execute(
            select(OpenOrder.right, OpenOrder.strike, OpenOrder.expiry)
            .where(OpenOrder.symbol == symbol)
            .where(OpenOrder.asset_type == "OPTION")
        ).all()
    return {(r, s, e) for r, s, e in rows if r and s and e}


async def fetch_and_cache_chain(
    symbol: str, *, intent_override: str | None = None
) -> tuple[list[OptionQuote], float | None]:
    """Pull the option chain for ``symbol`` per its intent preset, write to cache.

    Shared between the live ``advise_symbol`` flow and the scheduler's
    background pre-fetch job. Returns ``(quotes, spot)``; either may be empty
    if the symbol isn't scannable, no preset matches, or IBKR returned nothing.
    """
    presets = load_intents()
    sym = _load_symbol(symbol)

    intent = (intent_override or (sym.intent if sym else None) or "").upper()
    if intent not in SCANNABLE_INTENTS:
        return [], None

    preset = presets.get(intent)
    if preset is None:
        logger.error("No preset for intent %r in intents.yaml", intent)
        return [], None

    target = sym.target_buy_price if sym else None
    if intent == "WANT_TO_OWN" and target is None:
        return [], None

    strike_max = None
    if intent == "WANT_TO_OWN" and target is not None and preset.strike_max_vs_target:
        strike_max = target * preset.strike_max_vs_target

    accounts = load_accounts().accounts
    if not accounts:
        logger.error("No accounts configured in config/accounts.yaml")
        return [], None

    today = date.today()
    quotes: list[OptionQuote] = []
    spot: float | None = None
    async with MultiAccountClient([accounts[0]]) as multi:
        client = multi.clients[0]
        quotes = await client.fetch_option_chain(
            symbol,
            side=preset.side,
            dte_min=preset.dte_min,
            dte_max=preset.dte_max,
            today=today,
            strike_window_pct=preset.strike_window_pct,
            max_strikes_per_side=preset.max_strikes_per_side,
            strike_max=strike_max,
        )
        for q in quotes:
            if q.underlying_price is not None:
                spot = q.underlying_price
                break

    if quotes:
        _persist_chain_cache(quotes)
    return quotes, spot


async def fetch_and_cache_position_chain(
    symbol: str,
    right: str,
    *,
    dte_min: int = 7,
    dte_max: int = 120,
    strike_window_pct: float = 0.30,
    max_strikes_per_side: int = 25,
) -> list[OptionQuote]:
    """Pull chain for an open short's side/DTE band; complement intent prefetch.

    The intent preset only caches one side (PUT for WANT_TO_OWN, CALL for
    INCOME/TRADE). A WANT_TO_OWN symbol with an open short CALL therefore
    has no CALL quotes in cache, so the web detail pane's Roll simulator
    (which reads chain_cache) returns empty candidates.

    This helper is side- and DTE-aware: callers group open shorts by
    (symbol, right) and fetch once per group with a window wide enough to
    hold both the current leg and realistic roll candidates (≈ current
    expiry + 60d).
    """
    accounts = load_accounts().accounts
    if not accounts:
        return []

    today = date.today()
    async with MultiAccountClient([accounts[0]]) as multi:
        if not multi.clients:
            return []
        client = multi.clients[0]
        quotes = await client.fetch_option_chain(
            symbol,
            side="CALL" if right.upper() == "C" else "PUT",
            dte_min=dte_min,
            dte_max=dte_max,
            today=today,
            strike_window_pct=strike_window_pct,
            max_strikes_per_side=max_strikes_per_side,
        )
    if quotes:
        _persist_chain_cache(quotes)
    return quotes


def _persist_chain_cache(quotes: list[OptionQuote]) -> None:
    if not quotes:
        return
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for q in quotes:
            row = session.get(
                ChainCache,
                {"symbol": q.symbol, "expiry": q.expiry, "strike": q.strike, "right": q.right},
            )
            if row is None:
                row = ChainCache(
                    symbol=q.symbol, expiry=q.expiry, strike=q.strike, right=q.right
                )
                session.add(row)
            row.bid = q.bid
            row.ask = q.ask
            row.last = q.last
            row.delta = q.delta
            row.gamma = q.gamma
            row.theta = q.theta
            row.vega = q.vega
            row.iv = q.iv
            row.open_interest = q.open_interest
            row.volume = q.volume
            row.underlying_price = q.underlying_price
            row.fetched_at = now


def _persist_recommendations(symbol: str, intent: str, candidates: list[Candidate]) -> None:
    if not candidates:
        return
    with session_scope() as session:
        for rank, c in enumerate(candidates, start=1):
            session.add(
                Recommendation(
                    symbol=symbol,
                    intent=intent,
                    right=c.right,
                    strike=c.strike,
                    expiry=c.expiry,
                    premium=c.premium,
                    delta=c.delta,
                    dte=c.dte,
                    annualized_roc=c.annualized_roc,
                    rank=rank,
                )
            )


async def advise_symbol(
    symbol: str,
    *,
    intent_override: str | None = None,
) -> list[Candidate]:
    """Run the Opening Advisor end-to-end for a single symbol.

    Returns an empty list when:
      - the symbol is not tracked
      - the symbol's intent is not scannable (CORE_HOLD / WATCH)
      - the chain pull returned no usable quotes
    """
    presets = load_intents()
    sym = _load_symbol(symbol)

    intent = (intent_override or (sym.intent if sym else None) or "").upper()
    if intent not in SCANNABLE_INTENTS:
        logger.info("Symbol %s intent=%r is not scannable", symbol, intent)
        return []

    preset: IntentPreset | None = presets.get(intent)
    if preset is None:
        return []

    target = sym.target_buy_price if sym else None
    if intent == "WANT_TO_OWN" and target is None:
        logger.error("WANT_TO_OWN requires target_buy_price; tag with --target")
        return []

    today = date.today()
    earnings_dates = _load_earnings(symbol)

    quotes, spot = await fetch_and_cache_chain(symbol, intent_override=intent)

    if not quotes or spot is None:
        logger.warning("No usable quotes returned for %s", symbol)
        return []

    candidates = rank_chain(
        [_quote_to_filterable(q) for q in quotes],
        symbol=symbol,
        intent=intent,
        preset=preset,
        today=today,
        underlying_price=spot,
        earnings_dates=earnings_dates,
        target_buy_price=target,
        weekly_ok=bool(sym.weekly_ok) if sym else False,
    )

    # Drop candidates the user is already short OR has a pending order on —
    # no point recommending a contract that's open or in flight.
    held = _load_short_position_keys(symbol)
    pending = _load_pending_option_order_keys(symbol)
    excluded = held | pending
    if excluded:
        candidates = [
            c for c in candidates if (c.right, c.strike, c.expiry) not in excluded
        ]

    _persist_recommendations(symbol, intent, candidates)
    return candidates
