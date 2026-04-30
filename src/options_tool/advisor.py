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

import asyncio
import logging
from dataclasses import dataclass, field
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
from options_tool.domain.advisor_open import Candidate, rank_chain_with_rejections
from options_tool.domain.intents import (
    REASON_ALREADY_HELD,
    REASON_LABELS,
    REASON_PENDING_ORDER,
    SCANNABLE_INTENTS,
    FilterableQuote,
    Rejection,
    is_scannable,
)
from options_tool.ibkr import ChainFetchResult, MultiAccountClient, OptionQuote
from options_tool.settings import (
    IntentPreset,
    apply_preset_overrides,
    load_accounts,
    load_intents,
)
from options_tool.yahoo import fetch_chain_yahoo

# Exceptions raised when IB Gateway is unreachable. We catch these in the
# chain fetcher and either try Yahoo or surface a friendly reason — the prior
# behaviour was to bubble them up as 500s.
_IB_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    ConnectionError,        # ConnectionRefusedError / ConnectionResetError / etc.
    OSError,                # "Peer closed connection", etc.
)

logger = logging.getLogger(__name__)


# Sentinel PK used to store spot-only rows in chain_cache without touching
# schema. Any real option row has expiry >= today and strike > 0, so this
# cannot collide. Kept here (not jobs.py) because both the scheduler and the
# on-demand advisor write spot via this helper.
_SPOT_SENTINEL_EXPIRY = date(1970, 1, 1)
_SPOT_SENTINEL_STRIKE = 0.0
_SPOT_SENTINEL_RIGHT = "C"


def upsert_spot(symbol: str, price: float) -> None:
    """Write the latest spot for ``symbol`` to the chain_cache sentinel row.

    Used by the scheduler's spot-only watch path AND by ``fetch_and_cache_chain``
    when the chain itself returns empty (so the UI still has a price to show
    next to the 'no candidates' diagnostic).
    """
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        row = session.get(
            ChainCache,
            {
                "symbol": symbol,
                "expiry": _SPOT_SENTINEL_EXPIRY,
                "strike": _SPOT_SENTINEL_STRIKE,
                "right": _SPOT_SENTINEL_RIGHT,
            },
        )
        if row is None:
            row = ChainCache(
                symbol=symbol,
                expiry=_SPOT_SENTINEL_EXPIRY,
                strike=_SPOT_SENTINEL_STRIKE,
                right=_SPOT_SENTINEL_RIGHT,
            )
            session.add(row)
        row.underlying_price = price
        row.fetched_at = now


@dataclass(frozen=True, slots=True)
class RejectionGroup:
    """One reason bucket + the dropped contracts that share it.

    Sent to the UI so the user can drill into "why was nothing recommended".
    ``items`` is sorted by ``(expiry, strike)`` for stable display.
    """

    code: str
    label: str
    items: list[Rejection]

    @property
    def count(self) -> int:
        return len(self.items)


@dataclass(frozen=True, slots=True)
class AdviseResult:
    """Outcome of one ``advise_symbol`` call.

    ``reason`` explains *why* ``candidates`` is empty (e.g. spot far above
    target, intent not scannable, all candidates rejected by post-filters).
    ``spot`` carries the latest underlying price even when no candidates
    survived — UI displays it next to the diagnostic. ``source`` carries the
    upstream data source label ("ibkr" / "yahoo") so the UI can warn when
    we fell back to a degraded feed.
    ``rejection_groups`` is per-quote rejection rationale, aggregated by
    reason for the collapsible UI panel. ``quotes_total`` is the count of
    raw quotes pulled from upstream before any filter applied.
    """

    candidates: list[Candidate]
    spot: float | None
    reason: str | None = None
    source: str = "ibkr"
    rejection_groups: list[RejectionGroup] = field(default_factory=list)
    quotes_total: int = 0


def _build_rejection_groups(rejections: list[Rejection]) -> list[RejectionGroup]:
    """Bucket ``rejections`` by ``reason_code``, sort items + buckets.

    Buckets are sorted by ``count`` desc (then code asc for determinism).
    Items within a bucket are sorted by ``(expiry, strike)``.
    """
    by_code: dict[str, list[Rejection]] = {}
    for r in rejections:
        by_code.setdefault(r.reason_code, []).append(r)

    groups: list[RejectionGroup] = []
    for code, items in by_code.items():
        items.sort(key=lambda r: (r.expiry, r.strike))
        groups.append(
            RejectionGroup(
                code=code,
                label=REASON_LABELS.get(code, code),
                items=items,
            )
        )
    groups.sort(key=lambda g: (-g.count, g.code))
    return groups


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


def _load_open_short_legs(symbol: str) -> list[tuple[str, date, float]]:
    """Open short option legs on this symbol, as ``(right, expiry, strike)``.

    Used by ``advise_symbol`` to piggyback position-side quotes onto the
    intent-side chain fetch. The position legs almost always fall outside
    the intent preset's strike/DTE window, so without this they'd only get
    refreshed by the every-5-min scheduler — leaving OI / bid / ask stale
    on the open positions table right after the user clicks Run advisor.
    """
    today = date.today()
    with session_scope() as session:
        rows = session.execute(
            select(OptionPosition.right, OptionPosition.expiry, OptionPosition.strike)
            .where(OptionPosition.symbol == symbol)
            .where(OptionPosition.qty < 0)
        ).all()
    return [
        (r, e, float(s)) for r, e, s in rows
        if r and e and s and e >= today
    ]


async def fetch_and_cache_chain(
    symbol: str,
    *,
    intent_override: str | None = None,
    extra_contracts: list[tuple[str, date, float]] | None = None,
) -> tuple[list[OptionQuote], float | None, str | None, str]:
    """Pull the option chain for ``symbol`` per its intent preset, write to cache.

    Shared between the live ``advise_symbol`` flow and the scheduler's
    background pre-fetch job. Returns ``(quotes, spot, reason, source)`` —
    ``spot`` is populated whenever the underlying price was retrieved (even
    on empty chain) and is also persisted to the chain_cache sentinel row,
    so the UI has something to show. ``reason`` carries a user-facing
    diagnostic when ``quotes`` is empty. ``source`` is "ibkr" on the happy
    path, "yahoo" when we fell back, or "ibkr" with a connection-error
    reason when both failed.

    ``extra_contracts``: list of ``(right, expiry, strike)`` tuples to fetch
    in addition to the intent-side filter. Same connection, same OI pass —
    no extra latency. Used for open-position legs that fall outside the
    intent preset window.

    On IB connection failure (Gateway down, clientId collision, timeout) we
    transparently try Yahoo Finance as a degraded backup. Yahoo data is NOT
    written to ``chain_cache`` — its IV-derived BS Δ would otherwise mix with
    IB-grade Greeks downstream.
    """
    presets = load_intents()
    sym = _load_symbol(symbol)

    intent = (intent_override or (sym.intent if sym else None) or "").upper()
    if intent not in SCANNABLE_INTENTS:
        return [], None, f"intent {intent or '<未设置>'} 不可扫描", "ibkr"

    base_preset = presets.get(intent)
    if base_preset is None:
        logger.error("No preset for intent %r in intents.yaml", intent)
        return [], None, f"intents.yaml 缺 intent={intent} 的 preset", "ibkr"

    # Layer per-symbol overrides (filter knobs only) on top of the YAML default.
    preset = apply_preset_overrides(
        base_preset, sym.preset_overrides if sym else None
    )

    target = sym.target_buy_price if sym else None
    if intent == "WANT_TO_OWN" and target is None:
        return [], None, "WANT_TO_OWN 需先设置 target_buy_price", "ibkr"

    strike_max = None
    if intent == "WANT_TO_OWN" and target is not None and preset.strike_max_vs_target:
        strike_max = target * preset.strike_max_vs_target

    accounts = load_accounts().accounts
    if not accounts:
        logger.error("No accounts configured in config/accounts.yaml")
        return [], None, "config/accounts.yaml 未配置任何账户", "ibkr"

    today = date.today()

    # Primary: IBKR. On connection-level failure, fall through to Yahoo.
    ib_error: str | None = None
    try:
        async with MultiAccountClient([accounts[0]]) as multi:
            client = multi.clients[0]
            result = await client.fetch_option_chain(
                symbol,
                side=preset.side,
                dte_min=preset.dte_min,
                dte_max=preset.dte_max,
                today=today,
                strike_window_pct=preset.strike_window_pct,
                max_strikes_per_side=preset.max_strikes_per_side,
                strike_max=strike_max,
                extra_contracts=extra_contracts,
            )
    except _IB_CONNECTION_ERRORS as exc:
        ib_error = f"{type(exc).__name__}: {exc}" or type(exc).__name__
        logger.warning(
            "IB chain fetch failed for %s — %s; trying Yahoo fallback",
            symbol, ib_error,
        )
        result = None

    if result is None:
        # IB unreachable — degraded fallback.
        try:
            yahoo_result = await fetch_chain_yahoo(
                symbol,
                side=preset.side,
                dte_min=preset.dte_min,
                dte_max=preset.dte_max,
                today=today,
                strike_window_pct=preset.strike_window_pct,
                max_strikes_per_side=preset.max_strikes_per_side,
                strike_max=strike_max,
            )
        except Exception as yahoo_exc:
            logger.exception("Yahoo fallback also failed for %s", symbol)
            return (
                [], None,
                f"IB 不可用（{ib_error}）；Yahoo 也失败：{yahoo_exc}",
                "ibkr",
            )
        if yahoo_result.spot is not None:
            upsert_spot(symbol, yahoo_result.spot)
        return (
            yahoo_result.quotes,
            yahoo_result.spot,
            yahoo_result.reason,
            yahoo_result.source,
        )

    if result.quotes:
        _persist_chain_cache(result.quotes)
    elif result.spot is not None:
        # Chain came back empty but we did get a spot — persist it via the
        # sentinel row so the detail page can show "$XXX (cached)" next to
        # the diagnostic instead of a stale "spot 未缓存" hint.
        upsert_spot(symbol, result.spot)
    return result.quotes, result.spot, result.reason, result.source


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
    try:
        async with MultiAccountClient([accounts[0]]) as multi:
            if not multi.clients:
                return []
            client = multi.clients[0]
            result = await client.fetch_option_chain(
                symbol,
                side="CALL" if right.upper() == "C" else "PUT",
                dte_min=dte_min,
                dte_max=dte_max,
                today=today,
                strike_window_pct=strike_window_pct,
                max_strikes_per_side=max_strikes_per_side,
            )
    except _IB_CONNECTION_ERRORS as exc:
        # Roll candidate prefetch is best-effort; if IB is down the user just
        # sees an empty Roll table on the detail page. No Yahoo fallback here:
        # Roll simulator math depends on per-contract Δ/IV that BS-estimated
        # numbers would distort.
        logger.warning(
            "position-side chain fetch failed for %s %s — %s: %s",
            symbol, right, type(exc).__name__, exc,
        )
        return []
    if result.quotes:
        _persist_chain_cache(result.quotes)
    elif result.spot is not None:
        upsert_spot(symbol, result.spot)
    return result.quotes


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
            # OI / volume: don't clobber a prior cached value with None.
            # The streaming OI pass occasionally misses (rate-limit, slow
            # tick, stale subscription); preserving the last good number
            # is better than going dark.
            if q.open_interest is not None:
                row.open_interest = q.open_interest
            if q.volume is not None:
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
) -> AdviseResult:
    """Run the Opening Advisor end-to-end for a single symbol.

    Always returns an ``AdviseResult``. When ``candidates`` is empty, ``reason``
    explains why and ``spot`` (if obtained) is still populated for UI display.
    """
    presets = load_intents()
    sym = _load_symbol(symbol)

    intent = (intent_override or (sym.intent if sym else None) or "").upper()
    if intent not in SCANNABLE_INTENTS:
        logger.info("Symbol %s intent=%r is not scannable", symbol, intent)
        return AdviseResult(
            candidates=[],
            spot=None,
            reason=f"intent {intent or '<未设置>'} 不可扫描（仅 INCOME / TRADE / WANT_TO_OWN 会被扫描）",
        )

    base_preset: IntentPreset | None = presets.get(intent)
    if base_preset is None:
        return AdviseResult(
            candidates=[],
            spot=None,
            reason=f"intents.yaml 缺 intent={intent} 的 preset",
        )
    # Same merge rule as fetch_and_cache_chain — rank_chain reads delta_min /
    # delta_max etc., so it must see the per-symbol-overridden preset too.
    preset = apply_preset_overrides(
        base_preset, sym.preset_overrides if sym else None
    )

    target = sym.target_buy_price if sym else None
    if intent == "WANT_TO_OWN" and target is None:
        logger.error("WANT_TO_OWN requires target_buy_price; tag with --target")
        return AdviseResult(
            candidates=[],
            spot=None,
            reason="WANT_TO_OWN 需先设置 target_buy_price",
        )

    today = date.today()
    earnings_dates = _load_earnings(symbol)

    # Piggyback the open-position legs onto the chain fetch so their
    # bid/ask/Greeks/OI in chain_cache get refreshed alongside the
    # advisor's intent-side scan — same connection, no extra latency.
    extra_contracts = _load_open_short_legs(symbol)

    quotes, spot, fetch_reason, source = await fetch_and_cache_chain(
        symbol, intent_override=intent, extra_contracts=extra_contracts,
    )

    if not quotes:
        logger.warning(
            "No usable quotes returned for %s — %s",
            symbol, fetch_reason or "unknown",
        )
        return AdviseResult(
            candidates=[], spot=spot, reason=fetch_reason, source=source,
        )

    candidates, rejections = rank_chain_with_rejections(
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
    # no point recommending a contract that's open or in flight. Wrap each
    # drop as a Rejection so the UI's "why empty?" panel surfaces it too.
    held = _load_short_position_keys(symbol)
    pending = _load_pending_option_order_keys(symbol)
    if held or pending:
        kept: list[Candidate] = []
        for c in candidates:
            key = (c.right, c.strike, c.expiry)
            if key in held:
                rejections.append(
                    Rejection(
                        right=c.right, strike=c.strike, expiry=c.expiry,
                        dte=c.dte, delta=c.delta, mid=c.premium,
                        reason_code=REASON_ALREADY_HELD,
                        reason_detail="已是 open short — 不再重复推荐",
                    )
                )
                continue
            if key in pending:
                rejections.append(
                    Rejection(
                        right=c.right, strike=c.strike, expiry=c.expiry,
                        dte=c.dte, delta=c.delta, mid=c.premium,
                        reason_code=REASON_PENDING_ORDER,
                        reason_detail="该合约已有 working order（开仓 / 平仓）",
                    )
                )
                continue
            kept.append(c)
        candidates = kept

    _persist_recommendations(symbol, intent, candidates)

    rejection_groups = _build_rejection_groups(rejections)

    reason = None
    if not candidates:
        # We had quotes; nothing survived. Surface a short summary; the
        # template renders ``rejection_groups`` for the detailed breakdown.
        if rejection_groups:
            top = rejection_groups[0]
            reason = (
                f"拉到 {len(quotes)} 条 quote，全部被剔除 — "
                f"主因：{top.label}（{top.count} 条）。展开下面查看每条原因"
            )
        else:
            reason = (
                f"拉到 {len(quotes)} 条 quote，但全部被 intent 过滤器剔除。"
                "考虑在 intents.yaml 放宽 preset"
            )

    return AdviseResult(
        candidates=candidates,
        spot=spot,
        reason=reason,
        source=source,
        rejection_groups=rejection_groups,
        quotes_total=len(quotes),
    )
