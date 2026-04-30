"""IB Gateway adapter — connection, position sync, option chain fetching.

Every IBKR account in ``config/accounts.yaml`` corresponds to one Gateway
endpoint and one ``IB()`` client. ``MultiAccountClient`` wraps a fleet of them
so callers can fan out across accounts with ``asyncio.gather``.

Read-only by design: this module never sends orders. The user places orders
in TWS himself; we only observe.

Performance notes:
- Option chain pulls are gated by intent presets (DTE window + strike window
  around spot). A typical scan touches 30-80 contracts, not the whole chain.
- ``reqTickersAsync`` is the right primitive for snapshot-style data; subscribing
  per contract via ``reqMktData`` is necessary if we want streaming Greeks but
  costs more requests/sec budget.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Iterable

from ib_async import IB, Contract, ExecutionFilter, Index, Option, Stock, Ticker
from ib_async.ib import StartupFetch
from ib_async.objects import Position as IBPosition

from options_tool.settings import AccountConfig, load_accounts

logger = logging.getLogger(__name__)

# Strike window around spot. ±25% catches both ATM (TRADE intent) and far-OTM
# (INCOME intent, Delta ≤ 0.20) strikes for typical equity vols. Tune narrower
# for low-vol names if scan times become an issue.
_STRIKE_WINDOW_PCT = 0.25
_MAX_STRIKES_PER_SIDE = 20

# How long to let market data stream in before reading the snapshot.
# 4s is a safe default for liquid US equity options on IB Gateway.
_TICKER_WAIT_SECONDS = 4.0

# How long to let the streaming OI subscription run before cancelling.
# Open Interest ticks (generic 27/28) typically arrive within 1-2s; 3s
# is enough headroom without inflating chain-fetch latency.
_OI_STREAM_WAIT_SECONDS = 3.0


# Per-(host, port, clientId) connection lock. IB Gateway rejects a second
# connection on a clientId that's already in use (Error 326) — and we have
# at least two coroutines that compete for the same clientId: the web
# /advise route and the scheduler's prefetch_chains. Without this lock,
# whoever lost the race got a 10s TimeoutError.
#
# All FastAPI + APScheduler work runs in a single asyncio event loop so a
# plain ``asyncio.Lock`` (not thread-safe but loop-local) is sufficient.
_connection_locks: dict[tuple[str, int, int], asyncio.Lock] = {}


def _get_connection_lock(host: str, port: int, client_id: int) -> asyncio.Lock:
    key = (host, port, client_id)
    lock = _connection_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _connection_locks[key] = lock
    return lock


# ---- Data transfer objects -------------------------------------------------


@dataclass(frozen=True, slots=True)
class StockHolding:
    account_code: str
    symbol: str
    qty: float
    avg_cost: float
    market_value: float | None = None


@dataclass(frozen=True, slots=True)
class OptionHolding:
    account_code: str
    symbol: str
    right: str           # "C" or "P"
    strike: float
    expiry: date
    qty: int             # negative for short
    avg_open_price: float
    market_value: float | None = None


@dataclass(frozen=True, slots=True)
class OpenOrderRow:
    """One pending order pulled from IBKR. Stock or option."""

    account_code: str
    perm_id: int | None
    symbol: str
    asset_type: str          # "STOCK" or "OPTION"
    right: str | None        # "C"/"P" for options, None for stock
    strike: float | None
    expiry: date | None
    action: str              # "BUY" or "SELL"
    order_type: str          # "LMT", "STP", "MKT", ...
    qty: float
    lmt_price: float | None
    aux_price: float | None  # stop trigger price
    status: str | None


@dataclass(frozen=True, slots=True)
class ExecutionRow:
    """One historical fill from ``reqExecutions``.

    IBKR's API returns ~7 days of executions max; longer history requires
    Flex Query (XML reports). For the wheel ledger we sync incrementally
    and accept that pre-installation fills aren't backfillable.

    ``action`` is normalized to one of: BUY/SELL (stock) or
    STO/BTC/BTO/STC (options). Assignments arrive as $0 option fills with
    a paired stock leg — both are recorded; the cost basis math handles
    the pairing implicitly.
    """

    ib_exec_id: str
    account_code: str
    symbol: str
    asset_type: str         # "STOCK" | "OPTION"
    action: str             # BUY/SELL/STO/BTC/BTO/STC
    right: str | None
    strike: float | None
    expiry: date | None
    qty: float
    price: float            # per share for stock; per share (not per contract) for option
    commission: float
    executed_at: datetime


@dataclass(frozen=True, slots=True)
class StockCloseRow:
    """One day of TRADES close for an underlying — backs the recs analyzer."""

    symbol: str
    date: date
    close: float


@dataclass(frozen=True, slots=True)
class IVHistoryRow:
    """One day of historical IV/HV for an underlying.

    Sourced from IBKR ``reqHistoricalData`` with ``whatToShow=
    OPTION_IMPLIED_VOLATILITY`` (and HISTORICAL_VOLATILITY for HV). Both are
    annualized, expressed as fractions (0.42 = 42%).
    """

    symbol: str
    date: date
    iv_30d: float | None
    hv_30d: float | None


@dataclass(frozen=True, slots=True)
class ChainFetchResult:
    """Outcome of one ``fetch_option_chain`` call.

    ``reason`` is set whenever ``quotes`` is empty so the caller can surface
    a user-facing diagnostic instead of a generic "no candidates". ``spot`` is
    populated whenever the underlying spot fetch succeeded — even when the
    chain itself was filtered down to nothing — so the UI can still display
    the price and the user can sanity-check their target / strike window.
    ``source`` identifies where the data came from ("ibkr" by default, "yahoo"
    when the IB pull failed and we fell back) so the UI can show a degraded-mode
    banner.
    """

    quotes: list["OptionQuote"]
    spot: float | None
    reason: str | None
    source: str = "ibkr"


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """One option chain row with greeks. All numeric fields may be None when
    market data hasn't populated (illiquid strike, after-hours, etc.).
    """

    symbol: str
    expiry: date
    strike: float
    right: str
    bid: float | None
    ask: float | None
    last: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    iv: float | None
    open_interest: int | None
    volume: int | None
    underlying_price: float | None

    @property
    def mid(self) -> float | None:
        """Mid of bid/ask when available, else last (for after-hours)."""
        if (
            self.bid is not None
            and self.ask is not None
            and self.bid > 0
            and self.ask > 0
        ):
            return (self.bid + self.ask) / 2.0
        if self.last is not None and self.last > 0:
            return self.last
        return None


# ---- Per-account client ----------------------------------------------------


class IBClient:
    """Thin async wrapper around one ``ib_async.IB`` instance.

    Use as an async context manager so connect/disconnect is symmetric.
    """

    def __init__(self, cfg: AccountConfig) -> None:
        self.cfg = cfg
        self._ib = IB()
        self._lock_held = False

    async def __aenter__(self) -> "IBClient":
        await self.connect()
        return self

    async def __aexit__(self, *_exc) -> None:
        self.disconnect()

    async def connect(self, timeout: float = 10.0) -> None:
        # readonly=True + fetchFields=POSITIONS keeps us off the order/execution
        # endpoints. Default fetchFields would issue reqOpenOrders /
        # reqAutoOpenOrders / reqExecutions on every connect, which IB Gateway
        # in read-only mode flags as "API write permission required" with a
        # popup. We never place orders, so positions are all we need at startup.
        #
        # Acquire the per-clientId lock BEFORE connecting and hold it until
        # disconnect() — otherwise a second coroutine using the same clientId
        # collides with us mid-session and IB Gateway nukes the connection.
        lock = _get_connection_lock(
            self.cfg.host, self.cfg.port, self.cfg.client_id
        )
        await lock.acquire()
        self._lock_held = True
        try:
            await self._ib.connectAsync(
                self.cfg.host,
                self.cfg.port,
                clientId=self.cfg.client_id,
                timeout=timeout,
                readonly=True,
                fetchFields=StartupFetch.POSITIONS,
            )
        except BaseException:
            # Connect failed — release lock so the next caller can retry.
            self._lock_held = False
            lock.release()
            raise
        managed = self._ib.managedAccounts()
        if self.cfg.account_code not in managed:
            logger.warning(
                "Configured account %s not in gateway-managed list %s for alias=%s",
                self.cfg.account_code, managed, self.cfg.alias,
            )
        # Market data type 2 = "frozen": fall back to the last cached quote
        # when the market is closed. Live data (type 1) is still preferred
        # when available — IB silently uses live during RTH.
        self._ib.reqMarketDataType(2)

    def disconnect(self) -> None:
        if self._ib.isConnected():
            self._ib.disconnect()
        if self._lock_held:
            _get_connection_lock(
                self.cfg.host, self.cfg.port, self.cfg.client_id
            ).release()
            self._lock_held = False

    @property
    def ib(self) -> IB:
        return self._ib

    # ---- Positions --------------------------------------------------------

    async def fetch_positions(self) -> tuple[list[StockHolding], list[OptionHolding]]:
        """Return all stock + option positions for this account."""
        # ib_async caches positions; reqPositionsAsync forces a refresh.
        await self._ib.reqPositionsAsync()
        raw: list[IBPosition] = self._ib.positions(account=self.cfg.account_code)
        stocks: list[StockHolding] = []
        options: list[OptionHolding] = []
        for p in raw:
            c = p.contract
            if c.secType == "STK":
                stocks.append(
                    StockHolding(
                        account_code=p.account,
                        symbol=c.symbol,
                        qty=float(p.position),
                        avg_cost=float(p.avgCost),
                    )
                )
            elif c.secType == "OPT":
                expiry = _parse_ib_expiry(c.lastTradeDateOrContractMonth)
                options.append(
                    OptionHolding(
                        account_code=p.account,
                        symbol=c.symbol,
                        right=c.right,
                        strike=float(c.strike),
                        expiry=expiry,
                        qty=int(p.position),
                        avg_open_price=float(p.avgCost) / 100.0,  # IB reports per-contract
                    )
                )
            # other secTypes (CASH, FUT, BOND) are ignored for now
        return stocks, options

    # ---- Open orders ------------------------------------------------------

    async def fetch_open_orders(self) -> list[OpenOrderRow]:
        """Snapshot of currently-open orders for this account.

        Uses ``reqAllOpenOrdersAsync`` (one-shot snapshot of all orders on the
        account, regardless of which API client placed them). This is a read
        request — does NOT trigger the read-only Gateway popup the way the
        connect-time auto-subscriptions do.
        """
        trades = await self._ib.reqAllOpenOrdersAsync()
        out: list[OpenOrderRow] = []
        for t in trades:
            c = t.contract
            o = t.order
            # Filter to this account only — reqAllOpenOrders returns every
            # account managed by the Gateway login.
            if o.account and o.account != self.cfg.account_code:
                continue
            if c.secType == "STK":
                row = OpenOrderRow(
                    account_code=o.account or self.cfg.account_code,
                    perm_id=o.permId or None,
                    symbol=c.symbol,
                    asset_type="STOCK",
                    right=None,
                    strike=None,
                    expiry=None,
                    action=o.action,
                    order_type=o.orderType,
                    qty=float(o.totalQuantity),
                    lmt_price=float(o.lmtPrice) if o.lmtPrice else None,
                    aux_price=float(o.auxPrice) if o.auxPrice else None,
                    status=t.orderStatus.status if t.orderStatus else None,
                )
            elif c.secType == "OPT":
                row = OpenOrderRow(
                    account_code=o.account or self.cfg.account_code,
                    perm_id=o.permId or None,
                    symbol=c.symbol,
                    asset_type="OPTION",
                    right=c.right,
                    strike=float(c.strike),
                    expiry=_parse_ib_expiry(c.lastTradeDateOrContractMonth),
                    action=o.action,
                    order_type=o.orderType,
                    qty=float(o.totalQuantity),
                    lmt_price=float(o.lmtPrice) if o.lmtPrice else None,
                    aux_price=float(o.auxPrice) if o.auxPrice else None,
                    status=t.orderStatus.status if t.orderStatus else None,
                )
            else:
                continue
            out.append(row)
        return out

    # ---- Executions (transaction ledger) ---------------------------------

    async def fetch_executions(
        self, *, since: datetime | None = None
    ) -> list[ExecutionRow]:
        """Pull historical fills for this account.

        ``since`` is the lower-bound timestamp; default is "no filter" which
        IB interprets as "today's fills". For ledger backfill pass an explicit
        timestamp ~7 days back (IBKR's effective max via this API).

        Action classification:
          - STK BOT → BUY ; STK SLD → SELL
          - OPT SLD → STO (we only ever sell to open in this strategy)
          - OPT BOT → BTC (closing a short) — we don't open longs here

        This is a heuristic — if you ever buy long calls, the OPT BOT will
        be misclassified. Manual edit in the ``transactions`` table is the
        escape hatch.
        """
        ef = ExecutionFilter()
        if since is not None:
            ef.time = since.strftime("%Y%m%d-%H:%M:%S")
        fills = await self._ib.reqExecutionsAsync(ef)

        out: list[ExecutionRow] = []
        for fill in fills:
            ex = fill.execution
            c = fill.contract
            cr = fill.commissionReport

            if ex.acctNumber and ex.acctNumber != self.cfg.account_code:
                continue

            commission = float(cr.commission) if cr and cr.commission else 0.0
            executed_at = _parse_ib_timestamp(ex.time)

            if c.secType == "STK":
                action = "BUY" if ex.side == "BOT" else "SELL"
                row = ExecutionRow(
                    ib_exec_id=ex.execId,
                    account_code=ex.acctNumber or self.cfg.account_code,
                    symbol=c.symbol,
                    asset_type="STOCK",
                    action=action,
                    right=None,
                    strike=None,
                    expiry=None,
                    qty=float(ex.shares),
                    price=float(ex.price),
                    commission=commission,
                    executed_at=executed_at,
                )
            elif c.secType == "OPT":
                action = "STO" if ex.side == "SLD" else "BTC"
                row = ExecutionRow(
                    ib_exec_id=ex.execId,
                    account_code=ex.acctNumber or self.cfg.account_code,
                    symbol=c.symbol,
                    asset_type="OPTION",
                    action=action,
                    right=c.right,
                    strike=float(c.strike),
                    expiry=_parse_ib_expiry(c.lastTradeDateOrContractMonth),
                    qty=float(ex.shares),
                    price=float(ex.price),
                    commission=commission,
                    executed_at=executed_at,
                )
            else:
                continue
            out.append(row)
        return out

    # ---- Spot price -------------------------------------------------------

    async def fetch_spot(self, symbol: str) -> float | None:
        """Get a snapshot last/mid price for an underlying stock."""
        stock = Stock(symbol, "SMART", "USD")
        await self._ib.qualifyContractsAsync(stock)
        tickers = await self._ib.reqTickersAsync(stock)
        if not tickers:
            return None
        t = tickers[0]
        for candidate in (t.last, t.close, t.marketPrice()):
            if candidate is not None and candidate == candidate and candidate > 0:
                return float(candidate)
        return None

    # ---- Historical IV / HV -----------------------------------------------

    async def fetch_iv_history(
        self, symbol: str, *, lookback: str = "1 Y"
    ) -> list[IVHistoryRow]:
        """Pull daily implied + historical vol for ``symbol``.

        Two ``reqHistoricalDataAsync`` calls (IV + HV), zipped on date. IB's
        ``whatToShow="OPTION_IMPLIED_VOLATILITY"`` returns the underlying's
        ATM 30D-equivalent IV — exactly what we need for IV rank / percentile.

        ``lookback`` accepts IB's duration format: "1 Y", "6 M", "252 D".
        """
        underlying = Stock(symbol, "SMART", "USD")
        await self._ib.qualifyContractsAsync(underlying)

        async def _bars(what: str):
            return await self._ib.reqHistoricalDataAsync(
                underlying,
                endDateTime="",
                durationStr=lookback,
                barSizeSetting="1 day",
                whatToShow=what,
                useRTH=True,
                formatDate=1,
            )

        iv_bars, hv_bars = await asyncio.gather(
            _bars("OPTION_IMPLIED_VOLATILITY"),
            _bars("HISTORICAL_VOLATILITY"),
        )

        hv_by_date = {b.date: float(b.close) for b in hv_bars if b.close is not None and b.close > 0}
        out: list[IVHistoryRow] = []
        for b in iv_bars:
            if b.close is None or b.close <= 0:
                continue
            out.append(
                IVHistoryRow(
                    symbol=symbol,
                    date=b.date,
                    iv_30d=float(b.close),
                    hv_30d=hv_by_date.get(b.date),
                )
            )
        return out

    # ---- Stock price history ---------------------------------------------

    async def fetch_stock_closes(
        self, symbol: str, *, lookback: str = "1 Y"
    ) -> list[StockCloseRow]:
        """Pull daily TRADES close for ``symbol``.

        Consumed by the recommendation feedback-loop analyzer to answer "what
        would a not-taken recommendation have paid out?" — we only need the
        close on each recommendation's expiry date.

        ``lookback`` follows IB's duration format ("1 Y", "6 M", "90 D").
        """
        underlying = Stock(symbol, "SMART", "USD")
        await self._ib.qualifyContractsAsync(underlying)
        bars = await self._ib.reqHistoricalDataAsync(
            underlying,
            endDateTime="",
            durationStr=lookback,
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        out: list[StockCloseRow] = []
        for b in bars:
            if b.close is None or b.close <= 0:
                continue
            out.append(StockCloseRow(symbol=symbol, date=b.date, close=float(b.close)))
        return out

    # ---- Option chain -----------------------------------------------------

    async def fetch_option_chain(
        self,
        symbol: str,
        *,
        side: str,                  # "CALL" or "PUT"
        dte_min: int,
        dte_max: int,
        today: date | None = None,
        strike_window_pct: float = _STRIKE_WINDOW_PCT,
        max_strikes_per_side: int = _MAX_STRIKES_PER_SIDE,
        strike_max: float | None = None,
        extra_contracts: list[tuple[str, date, float]] | None = None,
    ) -> ChainFetchResult:
        """Fetch a filtered option chain for ``symbol``.

        - ``side``: "CALL" for CC scans, "PUT" for CSP scans.
        - ``dte_min`` / ``dte_max``: select expiries inside this window.
        - ``strike_window_pct``: keep strikes within ±X% of spot.
        - ``strike_max``: hard upper bound on strike (used by WANT_TO_OWN).
        - ``extra_contracts``: list of ``(right, expiry, strike)`` tuples to
          ALSO fetch quotes for, in addition to the intent-side filter. Used
          by the advisor to refresh open-position legs (which often fall
          outside the intent's strike/DTE window) under the same connection
          and OI pass — same latency as the base fetch.

        Returns a ``ChainFetchResult`` carrying ``spot`` even when the chain
        is filtered down to nothing — callers (advisor, UI) need the price to
        explain why no strikes survived.
        """
        today = today or datetime.now(timezone.utc).date()

        # 1) Resolve underlying contract + spot
        underlying = Stock(symbol, "SMART", "USD")
        await self._ib.qualifyContractsAsync(underlying)
        spot = await self.fetch_spot(symbol)
        if spot is None:
            logger.warning("No spot price for %s — chain fetch aborted", symbol)
            return ChainFetchResult(
                quotes=[],
                spot=None,
                reason="无法获取 spot — IB Gateway 未返回价格（market closed 且无 frozen 数据？）",
            )

        # 2) Get available expirations + strikes from secDef
        params_list = await self._ib.reqSecDefOptParamsAsync(
            underlying.symbol, "", underlying.secType, underlying.conId
        )
        if not params_list:
            logger.warning("No option params for %s", symbol)
            return ChainFetchResult(
                quotes=[], spot=spot, reason="IB 未返回 secDef option params"
            )
        # Prefer SMART exchange row when available.
        params = next((p for p in params_list if p.exchange == "SMART"), params_list[0])

        right = "C" if side.upper() == "CALL" else "P"

        # 3) Filter expirations by DTE window
        candidate_expiries: list[date] = []
        for exp_str in sorted(params.expirations):
            exp = _parse_ib_expiry(exp_str)
            dte = (exp - today).days
            if dte_min <= dte <= dte_max:
                candidate_expiries.append(exp)

        if not candidate_expiries:
            logger.info(
                "No expirations in DTE window [%d,%d] for %s", dte_min, dte_max, symbol
            )
            return ChainFetchResult(
                quotes=[],
                spot=spot,
                reason=f"DTE 窗口 [{dte_min},{dte_max}] 内无可用 expiry",
            )

        # 4) Filter strikes
        all_strikes = sorted(params.strikes)
        candidate_strikes = _select_strikes(
            all_strikes,
            spot=spot,
            window_pct=strike_window_pct,
            max_per_side=max_strikes_per_side,
            strike_max=strike_max,
            side=side,
        )
        if not candidate_strikes and not extra_contracts:
            # Truly empty: intent filtered everything out AND no extra legs
            # were piggybacked. Explain why.
            reason = _explain_empty_strikes(
                side=side,
                spot=spot,
                window_pct=strike_window_pct,
                strike_max=strike_max,
            )
            logger.info(
                "No candidate strikes for %s %s (spot=%.2f window=±%.0f%% strike_max=%s)",
                symbol, side, spot, strike_window_pct * 100,
                f"{strike_max:.2f}" if strike_max is not None else "—",
            )
            return ChainFetchResult(quotes=[], spot=spot, reason=reason)
        # Intent-side dropped everything but extra_contracts still has work
        # to do (e.g. WANT_TO_OWN strike-cap excludes everything yet user has
        # open shorts to refresh). Fall through with empty candidate_strikes.

        # 5) Build + qualify Option contracts. Dedupe (right, expiry, strike)
        # so an extra_contracts entry that already overlaps the intent grid
        # doesn't get fetched twice.
        seen: set[tuple[str, date, float]] = set()
        contracts: list[Option] = []
        for exp in candidate_expiries:
            for strike in candidate_strikes:
                key = (right, exp, float(strike))
                if key in seen:
                    continue
                seen.add(key)
                contracts.append(
                    Option(symbol, exp.strftime("%Y%m%d"), strike, right, "SMART")
                )
        for r, exp, strike in (extra_contracts or []):
            key = (r, exp, float(strike))
            if key in seen:
                continue
            seen.add(key)
            contracts.append(
                Option(symbol, exp.strftime("%Y%m%d"), float(strike), r, "SMART")
            )

        qualified = await self._ib.qualifyContractsAsync(*contracts)
        qualified = [c for c in qualified if getattr(c, "conId", 0)]
        if not qualified:
            return ChainFetchResult(
                quotes=[],
                spot=spot,
                reason="IB 未能 qualify 任何 option contract（合约可能已下架）",
            )

        # 6) Two-pass data fetch — IB doesn't let us get both in one call:
        #   Pass 1 (frozen, type=2): ``reqTickersAsync`` snapshot →
        #       bid/ask/last/Greeks. Frozen mode is essential for after-hours
        #       use because it returns the cached last quote instead of -1.
        #   Pass 2 (live, type=1): ``reqMktData(genericTickList="100,101",
        #       snapshot=False)`` streaming → call/putOpenInterest. Frozen
        #       mode SUPPRESSES generic ticks (including OI), so we must
        #       flip to live for this pass; the price tick fields will be
        #       -1 after-hours but we only care about OI here. Type is
        #       restored to frozen at the end so subsequent calls behave.
        # Sequential not parallel: ``reqTickersAsync`` auto-cancels its
        # underlying mktData subscription, which would tear down our OI
        # stream if it were already attached to the same contract.
        snapshot_tickers = await self._ib.reqTickersAsync(*qualified)
        await asyncio.sleep(_TICKER_WAIT_SECONDS)

        # CRITICAL: snapshot Tickers are mutable and IB keeps pushing updates
        # into them. The moment we flip to live (type=1) for the OI pass,
        # IB overwrites bid/ask with -1 on the same Ticker objects (no live
        # quote after-hours). Freeze the snapshot into immutable
        # ``OptionQuote`` dataclasses BEFORE the mode flip.
        snapshot_quotes: dict[int, OptionQuote] = {}
        for t in snapshot_tickers:
            if t.contract is None:
                continue
            con_id = getattr(t.contract, "conId", 0)
            if not con_id:
                continue
            snapshot_quotes[con_id] = _ticker_to_quote(t, symbol, spot)

        oi_by_con: dict[int, int] = {}
        self._ib.reqMarketDataType(1)
        try:
            oi_streams = [
                self._ib.reqMktData(c, genericTickList="100,101", snapshot=False)
                for c in qualified
            ]
            try:
                await asyncio.sleep(_OI_STREAM_WAIT_SECONDS)
                for t in oi_streams:
                    if t.contract is None:
                        continue
                    cc = t.contract
                    raw = (
                        t.callOpenInterest if cc.right == "C"
                        else t.putOpenInterest
                    )
                    if raw is None:
                        continue
                    try:
                        val = float(raw)
                    except (TypeError, ValueError):
                        continue
                    if val != val or val < 0:  # NaN or sentinel
                        continue
                    oi_by_con[cc.conId] = int(val)
            finally:
                for c in qualified:
                    try:
                        self._ib.cancelMktData(c)
                    except Exception:
                        # Best-effort cancel — losing one slot is preferable
                        # to crashing the whole fetch.
                        logger.exception("cancelMktData failed for %s", c)
        finally:
            # Always restore frozen so the next fetch (and any concurrent
            # consumer like position sync) sees the connection's expected
            # mode. Skipping this once leaks "live mode" into the rest of
            # the session.
            self._ib.reqMarketDataType(2)

        # Merge: snapshot_quotes already has bid/ask/Greeks; overlay OI.
        quotes: list[OptionQuote] = []
        for con_id, q in snapshot_quotes.items():
            if q.open_interest is None and con_id in oi_by_con:
                q = replace(q, open_interest=oi_by_con[con_id])
            quotes.append(q)
        return ChainFetchResult(quotes=quotes, spot=spot, reason=None)


# ---- Multi-account fanout --------------------------------------------------


class MultiAccountClient:
    """Holds one ``IBClient`` per enabled account. Fans out reads concurrently."""

    def __init__(self, configs: Iterable[AccountConfig] | None = None) -> None:
        cfgs = list(configs) if configs is not None else load_accounts().accounts
        self._clients: list[IBClient] = [IBClient(c) for c in cfgs if c.enabled]

    @property
    def clients(self) -> list[IBClient]:
        return self._clients

    async def __aenter__(self) -> "MultiAccountClient":
        await asyncio.gather(*(c.connect() for c in self._clients))
        return self

    async def __aexit__(self, *_exc) -> None:
        # Disconnect every client even if one of them raises — otherwise we'd
        # leak the per-clientId connection lock for any client past the failure
        # point, and the next /advise call would block forever.
        for c in self._clients:
            try:
                c.disconnect()
            except Exception:
                logger.exception("disconnect failed for clientId=%s", c.cfg.client_id)

    async def fetch_all_positions(
        self,
    ) -> tuple[list[StockHolding], list[OptionHolding]]:
        results = await asyncio.gather(*(c.fetch_positions() for c in self._clients))
        stocks: list[StockHolding] = []
        options: list[OptionHolding] = []
        for s, o in results:
            stocks.extend(s)
            options.extend(o)
        return stocks, options

    async def fetch_iv_history(
        self, symbol: str, *, lookback: str = "1 Y"
    ) -> list[IVHistoryRow]:
        """One Gateway is enough — IV is a market-wide property, not per-account."""
        if not self._clients:
            return []
        return await self._clients[0].fetch_iv_history(symbol, lookback=lookback)

    async def fetch_stock_closes(
        self, symbol: str, *, lookback: str = "1 Y"
    ) -> list[StockCloseRow]:
        """One Gateway is enough — closes are market-wide."""
        if not self._clients:
            return []
        return await self._clients[0].fetch_stock_closes(symbol, lookback=lookback)

    async def fetch_all_executions(
        self, *, since: datetime | None = None
    ) -> list[ExecutionRow]:
        results = await asyncio.gather(
            *(c.fetch_executions(since=since) for c in self._clients)
        )
        out: list[ExecutionRow] = []
        for rows in results:
            out.extend(rows)
        return out

    async def fetch_all_open_orders(self) -> list[OpenOrderRow]:
        results = await asyncio.gather(*(c.fetch_open_orders() for c in self._clients))
        out: list[OpenOrderRow] = []
        for rows in results:
            out.extend(rows)
        return out


# ---- Internal helpers ------------------------------------------------------


def _parse_ib_timestamp(s: str) -> datetime:
    """IB execution times look like ``20260417  10:23:45`` or
    ``20260417 10:23:45 US/Eastern``. Coerce to UTC datetime.

    Falls back to "now" if parsing fails — better than crashing the sync.
    """
    if not s:
        return datetime.now(timezone.utc)
    parts = s.split()
    try:
        if len(parts) >= 2:
            dt = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y%m%d %H:%M:%S")
        else:
            dt = datetime.strptime(parts[0], "%Y%m%d")
        if len(parts) >= 3:
            try:
                from zoneinfo import ZoneInfo
                dt = dt.replace(tzinfo=ZoneInfo(parts[2]))
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning("Could not parse IB exec timestamp %r; using now()", s)
        return datetime.now(timezone.utc)


def _parse_ib_expiry(s: str) -> date:
    """IB returns expiries as YYYYMMDD or YYYYMM. Coerce to a date."""
    if len(s) == 8:
        return datetime.strptime(s, "%Y%m%d").date()
    if len(s) == 6:
        return datetime.strptime(s + "01", "%Y%m%d").date()
    raise ValueError(f"Unrecognized IB expiry format: {s!r}")


def _explain_empty_strikes(
    *,
    side: str,
    spot: float,
    window_pct: float,
    strike_max: float | None,
) -> str:
    """User-facing explanation when ``_select_strikes`` returned nothing.

    The most common WANT_TO_OWN failure: spot has run far above target, so
    the lower bound of the strike window (``spot * (1 - window_pct)``) is
    already above ``strike_max`` (= target × cap). We compute that explicitly
    so the user knows whether to bump the target or widen the window.
    """
    if side.upper() == "PUT" and strike_max is not None:
        lower = spot * (1.0 - window_pct)
        if lower > strike_max:
            return (
                f"spot ${spot:.2f} 已远离 target — strike_window 下沿 "
                f"${lower:.2f} > strike_max ${strike_max:.2f}（target × cap）。"
                f"考虑提高 target 或加大 strike_window_pct"
            )
        return (
            f"strike_max ${strike_max:.2f} 与 spot ${spot:.2f} ±{window_pct*100:.0f}% "
            f"窗口的交集中没有可用 strike"
        )
    return (
        f"spot ${spot:.2f} ±{window_pct*100:.0f}% 窗口内无可用 strike"
    )


def _select_strikes(
    strikes: list[float],
    *,
    spot: float,
    window_pct: float,
    max_per_side: int,
    strike_max: float | None,
    side: str,
) -> list[float]:
    """Pick the strikes worth pulling from the chain.

    For CALL (CC): keep strikes within +window_pct of spot, up to max_per_side
    strikes above spot (we sell OTM calls; ITM is uncommon for CC).
    For PUT (CSP): keep strikes within -window_pct of spot, up to max_per_side
    strikes below spot, capped at strike_max if provided.
    """
    upper = spot * (1.0 + window_pct)
    lower = spot * (1.0 - window_pct)
    if side.upper() == "CALL":
        eligible = [s for s in strikes if spot <= s <= upper]
        eligible.sort()
        return eligible[:max_per_side]
    # PUT
    eligible = [s for s in strikes if lower <= s <= spot]
    if strike_max is not None:
        eligible = [s for s in eligible if s <= strike_max]
    eligible.sort(reverse=True)  # closest to spot first
    return sorted(eligible[:max_per_side])


def _ticker_to_quote(t: Ticker, symbol: str, spot: float) -> OptionQuote:
    c = t.contract
    expiry = _parse_ib_expiry(c.lastTradeDateOrContractMonth)

    # Greeks come from one of three computation slots (model, ask, bid). Prefer
    # modelGreeks (implied) but fall back if missing.
    greeks = t.modelGreeks or t.lastGreeks or t.askGreeks or t.bidGreeks

    def _safe(v):
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        # IB sends NaN for unavailable values
        return f if f == f else None

    raw_oi = t.callOpenInterest if c.right == "C" else t.putOpenInterest
    oi_val = _safe(raw_oi)
    vol_val = _safe(t.volume)

    return OptionQuote(
        symbol=symbol,
        expiry=expiry,
        strike=float(c.strike),
        right=c.right,
        bid=_safe(t.bid),
        ask=_safe(t.ask),
        last=_safe(t.last),
        delta=_safe(greeks.delta) if greeks else None,
        gamma=_safe(greeks.gamma) if greeks else None,
        theta=_safe(greeks.theta) if greeks else None,
        vega=_safe(greeks.vega) if greeks else None,
        iv=_safe(greeks.impliedVol) if greeks else None,
        open_interest=int(oi_val) if oi_val is not None else None,
        volume=int(vol_val) if vol_val is not None else None,
        underlying_price=spot,
    )
