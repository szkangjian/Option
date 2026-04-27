"""Command-line interface for options-tool.

启动入口为主，日常操作走 Web 面板。

Commands:
  init-db          创建 / 升级 SQLite schema
  sync-positions   从 IBKR 拉持仓到本地 DB（一般用 Web 的 Sync 按钮）
  serve            启动 Web 面板
  version          打印版本号
"""
from __future__ import annotations

import asyncio

import typer
from rich.console import Console

from options_tool import __version__
from options_tool.db import init_db as _init_db
from options_tool.logging_setup import configure_logging
from options_tool.sync import (
    sync_earnings,
    sync_iv_history,
    sync_positions,
    sync_stock_closes_for_recs,
    sync_transactions,
)

app = typer.Typer(
    add_completion=False,
    help="Local-first options trading assistant for Interactive Brokers.",
    no_args_is_help=True,
)
symbols_app = typer.Typer(
    help="Symbol 的 intent / target / wheel / weekly 管理（Web 面板也能做）。",
    no_args_is_help=True,
)
app.add_typer(symbols_app, name="symbols")
console = Console()


@app.command("init-db")
def init_db_cmd(
    reset: bool = typer.Option(
        False, "--reset", help="Drop existing tables before creating."
    ),
) -> None:
    """创建 / 升级 SQLite schema。"""
    configure_logging()
    _init_db(drop_first=reset)
    console.print("[green]✓[/green] schema ready")


@app.command("sync-positions")
def sync_positions_cmd() -> None:
    """从 IBKR 拉持仓到本地 DB（debug 用，日常用 Web 的 Sync 按钮）。"""
    configure_logging()
    stock_n, option_n = asyncio.run(sync_positions())
    console.print(
        f"[green]✓[/green] synced {stock_n} stock positions, {option_n} option positions"
    )


@app.command("sync-earnings")
def sync_earnings_cmd() -> None:
    """单独拉一次 Finnhub 财报日期（debug 用）。"""
    configure_logging()
    n = asyncio.run(sync_earnings())
    console.print(f"[green]✓[/green] fetched {n} earnings rows")


@app.command("sync-transactions")
def sync_transactions_cmd(
    days: int = typer.Option(7, "--days", help="回拉天数（IBKR API 上限约 7 天）"),
) -> None:
    """从 IBKR 拉历史 fills 进 transactions 表（按 ib_exec_id dedupe）。"""
    configure_logging()
    n = asyncio.run(sync_transactions(lookback_days=days))
    console.print(f"[green]✓[/green] inserted {n} new transactions")


@app.command("sync-iv-history")
def sync_iv_history_cmd(
    symbol: str | None = typer.Argument(None, help="单只 symbol；省略则全部 tracked"),
    backfill: bool = typer.Option(False, "--backfill", help="强制 1 年回填，不管已有数据"),
) -> None:
    """拉历史 IV/HV bars 进 iv_history 表（首次自动 1 年回填，之后增量 10 天）。"""
    configure_logging()
    syms = [symbol.upper()] if symbol else None
    lookback = "1 Y" if backfill else None
    n = asyncio.run(sync_iv_history(syms, lookback=lookback))
    console.print(f"[green]✓[/green] upserted {n} IV history rows")


@app.command("test-telegram")
def test_telegram_cmd(
    message: str = typer.Argument("✅ options-tool Telegram 测试通过"),
) -> None:
    """发一条测试消息验证 token / chat_id 是否配置正确。"""
    configure_logging()
    from options_tool.alerts import _send_telegram
    from options_tool.settings import get_settings

    s = get_settings()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        console.print("[red]✗[/red] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 未配置")
        raise typer.Exit(1)
    ok = asyncio.run(_send_telegram(s.telegram_bot_token, s.telegram_chat_id, message))
    if ok:
        console.print("[green]✓[/green] sent")
    else:
        console.print("[red]✗[/red] send failed (查看日志)")
        raise typer.Exit(1)


@app.command("scan-alerts")
def scan_alerts_cmd() -> None:
    """手动跑一次告警扫描（debug 用，平常 scheduler 自动跑）。"""
    configure_logging()
    from options_tool.jobs import scan_alerts

    asyncio.run(scan_alerts())
    console.print("[green]✓[/green] scan complete")


@app.command("serve")
def serve_cmd(
    host: str = typer.Option(None),
    port: int = typer.Option(None),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """启动 Web 面板。"""
    import uvicorn

    from options_tool.settings import get_settings

    # Install our log filters BEFORE uvicorn boots — otherwise ib_async's
    # "Error 200 / Unknown contract" chatter from chain pulls floods the
    # terminal during every advisor run.
    configure_logging()

    s = get_settings()
    uvicorn.run(
        "options_tool.web.app:app",
        host=host or s.web_host,
        port=port or s.web_port,
        reload=reload,
    )


@app.command("simulate-roll")
def simulate_roll_cmd(
    symbol: str = typer.Argument(..., help="underlying ticker, e.g. URA"),
    right: str = typer.Option(..., "--right", help="C or P"),
    strike: float = typer.Option(..., "--strike"),
    expiry: str = typer.Option(..., "--expiry", help="YYYY-MM-DD"),
    dte_min: int = typer.Option(7, "--dte-min"),
    dte_max: int = typer.Option(120, "--dte-max"),
    delta_ceiling: float = typer.Option(
        None, "--delta-ceiling",
        help="overrides AlertsConfig.delta_warning for the simulator's defense filter",
    ),
    top_n: int = typer.Option(10, "--top"),
    strike_window_pct: float = typer.Option(0.25, "--strike-window-pct"),
) -> None:
    """拉新 chain + 跑 roll simulator。输出 top-N 候选，按 net credit 排序。

    Live IBKR pull (替代一次性 ``scripts/roll_ura.py``)。
    """
    from datetime import date as _date, datetime as _datetime

    from options_tool.domain.alert_detection import ShortPositionSnapshot
    from options_tool.domain.roll_simulator import (
        RollQuote, simulate_rolls,
    )
    from options_tool.ibkr import MultiAccountClient
    from options_tool.settings import AlertsConfig, load_accounts, load_alerts

    configure_logging()
    side = right.upper()
    if side not in ("C", "P"):
        console.print("[red]✗[/red] --right must be C or P")
        raise typer.Exit(1)
    try:
        pos_expiry = _datetime.strptime(expiry, "%Y-%m-%d").date()
    except ValueError:
        console.print("[red]✗[/red] --expiry must be YYYY-MM-DD")
        raise typer.Exit(1)

    accounts = load_accounts().accounts
    if not accounts:
        console.print("[red]✗[/red] no accounts in config/accounts.yaml")
        raise typer.Exit(1)

    today = _date.today()
    alerts_cfg = load_alerts()
    if delta_ceiling is not None:
        alerts_cfg = AlertsConfig(
            **{**alerts_cfg.model_dump(), "delta_warning": delta_ceiling}
        )

    async def _run() -> None:
        async with MultiAccountClient([accounts[0]]) as multi:
            if not multi.clients:
                console.print("[red]✗[/red] no connected IBKR clients")
                raise typer.Exit(2)
            client = multi.clients[0]
            quotes = await client.fetch_option_chain(
                symbol.upper(),
                side="CALL" if side == "C" else "PUT",
                dte_min=dte_min,
                dte_max=dte_max,
                today=today,
                strike_window_pct=strike_window_pct,
                max_strikes_per_side=25,
            )

        if not quotes:
            console.print("[yellow]no quotes returned — market closed or bad symbol[/yellow]")
            return

        current = next(
            (q for q in quotes
             if q.expiry == pos_expiry and q.strike == strike and q.right == side),
            None,
        )
        close_cost = None
        if current is not None:
            if current.ask is not None and current.ask > 0:
                close_cost = current.ask
            elif current.mid is not None:
                close_cost = current.mid
        if close_cost is None:
            console.print(
                f"[red]✗[/red] could not locate {side}{strike:g} {pos_expiry} in chain "
                f"— try widening --dte-min/--dte-max or --strike-window-pct"
            )
            raise typer.Exit(3)

        snap = ShortPositionSnapshot(
            symbol=symbol.upper(), right=side, strike=strike, expiry=pos_expiry,
            qty=-1, avg_open_price=None,
            current_mark=current.mid if current else None,
            current_delta=current.delta if current else None,
        )
        roll_quotes = [
            RollQuote(
                expiry=q.expiry, strike=q.strike, right=q.right,
                bid=q.bid, ask=q.ask, last=q.last, delta=q.delta,
            )
            for q in quotes if q is not current
        ]
        candidates = simulate_rolls(
            snap, close_cost, roll_quotes, today, alerts_cfg,
            top_n=top_n, min_dte=dte_min, max_dte=dte_max,
        )
        if not candidates:
            console.print(
                "[yellow]no viable candidates[/yellow] — "
                "relax --delta-ceiling, widen --strike-window-pct, or retry during RTH"
            )
            return

        from rich.table import Table

        t = Table(title=f"{symbol.upper()} {side}{strike:g} {pos_expiry} · close @ ${close_cost:.2f}")
        for c in ("expiry", "DTE", "strike", "Δstk", "Δ", "open", "net/sh", "net ×1 lot"):
            t.add_column(c, justify="right")
        for cand in candidates:
            t.add_row(
                cand.new_expiry.isoformat(),
                str(cand.new_dte),
                f"{cand.new_strike:g}",
                f"{cand.strike_diff:+g}",
                f"{cand.new_delta:.2f}" if cand.new_delta is not None else "—",
                f"${cand.open_credit:.2f}",
                f"${cand.net_credit:+.2f}",
                f"${cand.net_credit * 100:+,.0f}",
            )
        console.print(t)

    asyncio.run(_run())


@app.command("analyze-recommendations")
def analyze_recommendations_cmd(
    refresh: bool = typer.Option(
        False, "--refresh",
        help="First pull missing stock closes from IBKR (needs Gateway).",
    ),
    skip_scope: bool = typer.Option(
        False, "--skipped-only",
        help="Only analyze recs where taken=False (feedback loop mode).",
    ),
    symbol: str | None = typer.Option(
        None, "--symbol",
        help="Restrict analysis to one underlying (default: all).",
    ),
    since: str | None = typer.Option(
        None, "--since",
        help="Only include recommendations generated on/after YYYY-MM-DD.",
    ),
    top_outcomes: int = typer.Option(
        10, "--top",
        help="How many best / worst individual outcomes to print.",
    ),
) -> None:
    """反馈环分析 — 到期的 recommendation 如果当初采纳了，会赚多少？

    逻辑 (单 contract)：short option 持到到期，OOM → 保留全部 premium；
    ITM → premium - 内在价值。CC 的 ITM 仅算期权腿本身的亏损，不抵扣底层
    股票升值（分析关心的是选合约的水平，不是总盘 P&L）。

    ``--skipped-only`` 聚焦"没采纳的那些 Top-N 本来赚钱吗"这个校准问题。
    """
    from datetime import date as _date, datetime as _datetime

    from sqlalchemy import select as _select

    from options_tool.db import (
        Recommendation as _Rec,
        StockPriceHistory as _SPH,
        session_scope as _scope,
    )
    from options_tool.domain.feedback_loop import RecRow, analyze

    configure_logging()

    if refresh:
        console.print("[cyan]…[/cyan] pulling missing stock closes from IBKR")
        try:
            syms_n, rows_n = asyncio.run(sync_stock_closes_for_recs())
            console.print(
                f"[green]✓[/green] fetched {syms_n} symbols, inserted {rows_n} close rows"
            )
        except Exception as exc:
            console.print(f"[yellow]![/yellow] refresh failed ({exc}); proceeding with cached data")

    since_date = None
    if since:
        try:
            since_date = _datetime.strptime(since, "%Y-%m-%d").date()
        except ValueError:
            console.print("[red]✗[/red] --since must be YYYY-MM-DD")
            raise typer.Exit(1)

    today = _date.today()
    with _scope() as session:
        q = _select(_Rec)
        if symbol:
            q = q.where(_Rec.symbol == symbol.upper())
        if since_date:
            q = q.where(_Rec.generated_at >= _datetime.combine(since_date, _datetime.min.time()))
        raw_recs = list(session.scalars(q))
        if not raw_recs:
            console.print("[yellow]no recommendations in scope[/yellow]")
            return
        recs = [
            RecRow(
                id=r.id, symbol=r.symbol, intent=r.intent, right=r.right,
                strike=r.strike, expiry=r.expiry, premium=r.premium,
                delta=r.delta, dte=r.dte, annualized_roc=r.annualized_roc,
                rank=r.rank, taken=r.taken,
            )
            for r in raw_recs
        ]
        # Pre-filter closes to symbols we actually need.
        need_keys = {(r.symbol, r.expiry) for r in recs}
        close_rows = list(session.execute(
            _select(_SPH.symbol, _SPH.date, _SPH.close).where(
                _SPH.symbol.in_({k[0] for k in need_keys})
            )
        ))
    closes: dict[tuple[str, _date], float] = {
        (sym, d): close for sym, d, close in close_rows
    }
    report = analyze(recs, closes=closes, as_of=today, only_not_taken=skip_scope)

    from rich.table import Table
    console.print(
        f"[bold]Scope[/bold]: total={report.total_recs} · expired={report.expired_recs} · "
        f"priced={report.priced_recs} · missing={len(report.missing_prices)}"
    )
    if report.priced_recs == 0:
        if report.missing_prices:
            console.print(
                "[yellow]no priced outcomes yet — run with [/yellow][bold]--refresh[/bold]"
                "[yellow] while IB Gateway is up to fetch closes.[/yellow]"
            )
            for sym, exp in report.missing_prices[:20]:
                console.print(f"  missing: {sym} @ {exp}")
        return

    def _pct(x: float) -> str:
        return f"{x * 100:.0f}%"

    def _money(x: float) -> str:
        sign = "-" if x < 0 else ("+" if x > 0 else " ")
        return f"{sign}${abs(x):,.0f}"

    overall = report.overall
    console.print(
        f"\n[bold]Overall[/bold]: {overall.count} outcomes · hit-rate {_pct(overall.hit_rate)} · "
        f"total {_money(overall.total_pnl)} · avg {_money(overall.avg_pnl)}"
    )

    def _bucket_table(title: str, buckets) -> None:
        t = Table(title=title)
        for c in ("bucket", "n", "hit%", "avg P&L", "total P&L", "avg premium"):
            t.add_column(c, justify="right" if c != "bucket" else "left")
        for b in buckets:
            if b.count == 0:
                continue
            t.add_row(
                b.label, str(b.count), _pct(b.hit_rate),
                _money(b.avg_pnl), _money(b.total_pnl), _money(b.avg_premium),
            )
        console.print(t)

    _bucket_table("By intent", report.by_intent)
    _bucket_table("By rank (lower rank = more confident pick)", report.by_rank)
    _bucket_table("By symbol (sorted by total P&L)", report.by_symbol[:15])
    _bucket_table("By taken flag", report.by_taken)

    if report.outcomes:
        sorted_by_pnl = sorted(report.outcomes, key=lambda o: o.pnl_total)
        worst = sorted_by_pnl[:top_outcomes]
        best = list(reversed(sorted_by_pnl[-top_outcomes:]))

        def _outcome_table(title: str, rows) -> None:
            t = Table(title=title)
            for c in ("symbol", "intent", "leg", "strike", "expiry",
                      "close", "premium", "rank", "taken", "P&L"):
                t.add_column(c, justify="right" if c != "symbol" else "left")
            for o in rows:
                t.add_row(
                    o.symbol, o.intent, o.right, f"{o.strike:g}",
                    o.expiry.isoformat(), f"${o.close_at_expiry:.2f}",
                    f"${o.premium:.2f}", str(o.rank),
                    "✓" if o.taken else "·", _money(o.pnl_total),
                )
            console.print(t)

        _outcome_table(f"Top {len(best)} wins", best)
        _outcome_table(f"Bottom {len(worst)} losses", worst)

    if report.missing_prices:
        console.print(
            f"\n[dim]{len(report.missing_prices)} (symbol, expiry) pairs have no "
            f"cached close — rerun with --refresh to fill.[/dim]"
        )


@app.command()
def version() -> None:
    """打印版本号。"""
    console.print(__version__)


# ---- symbols subcommands ---------------------------------------------------


@symbols_app.command("list")
def symbols_list_cmd(
    show_hidden: bool = typer.Option(False, "--hidden", help="包含隐藏的 symbol"),
) -> None:
    """列出已跟踪的 symbol（含 intent / target / wheel / weekly）。"""
    from sqlalchemy import select

    from options_tool.db import Symbol, session_scope

    configure_logging()
    with session_scope() as session:
        q = select(Symbol).order_by(Symbol.symbol)
        if not show_hidden:
            q = q.where(Symbol.hidden == False)  # noqa: E712
        rows = session.scalars(q).all()
        if not rows:
            console.print("[yellow]no symbols tracked[/yellow]")
            return
        from rich.table import Table

        table = Table(show_edge=False, pad_edge=False)
        for col in ("symbol", "intent", "target", "wheel", "weekly", "hidden", "notes"):
            table.add_column(col, overflow="fold")
        for s in rows:
            table.add_row(
                s.symbol,
                s.intent,
                f"{s.target_buy_price:.2f}" if s.target_buy_price else "—",
                "✓" if s.wheel_enabled else "",
                "✓" if s.weekly_ok else "",
                "✓" if s.hidden else "",
                s.notes or "",
            )
        console.print(table)


@symbols_app.command("set")
def symbols_set_cmd(
    symbol: str = typer.Argument(..., help="ticker，e.g. URA"),
    intent: str = typer.Option(None, "--intent", help="CORE_HOLD|INCOME|TRADE|WANT_TO_OWN|WATCH"),
    target: float = typer.Option(None, "--target", help="target_buy_price（仅 WANT_TO_OWN 有意义）"),
    clear_target: bool = typer.Option(False, "--clear-target", help="清空 target_buy_price"),
    wheel: bool = typer.Option(None, "--wheel/--no-wheel", help="开关 wheel_enabled"),
    weekly: bool = typer.Option(None, "--weekly/--no-weekly", help="是否允许非 3rd-Friday 到期"),
    hidden: bool = typer.Option(None, "--hide/--unhide"),
    notes: str = typer.Option(None, "--notes"),
) -> None:
    """修改已跟踪 symbol 的字段；不存在则退出。创建新 symbol 走 Web 的 + Add。"""
    from options_tool.db import INTENT_VALUES, Symbol, session_scope

    configure_logging()
    sym_key = symbol.upper()
    if intent is not None and intent.upper() not in INTENT_VALUES:
        console.print(f"[red]invalid intent[/red] — must be one of {INTENT_VALUES}")
        raise typer.Exit(1)
    with session_scope() as session:
        row = session.get(Symbol, sym_key)
        if row is None:
            console.print(f"[red]✗[/red] {sym_key} not tracked")
            raise typer.Exit(1)
        if intent is not None:
            row.intent = intent.upper()
        if clear_target:
            row.target_buy_price = None
        elif target is not None:
            row.target_buy_price = target
        if wheel is not None:
            row.wheel_enabled = wheel
        if weekly is not None:
            row.weekly_ok = weekly
        if hidden is not None:
            row.hidden = hidden
        if notes is not None:
            row.notes = notes or None
        # Warn (don't block) if post-edit state is WANT_TO_OWN without target.
        # The Web form enforces this at write, but CLI callers may edit
        # unrelated fields (notes, weekly) on a symbol that was already in
        # this broken state — refusing the save would be more annoying than
        # helpful. Advisor already no-ops silently; UI shows the ⚠ indicator.
        missing_target_warn = (
            row.intent == "WANT_TO_OWN" and row.target_buy_price is None
        )
    console.print(f"[green]✓[/green] {sym_key} updated")
    if missing_target_warn:
        console.print(
            "[yellow]⚠[/yellow] WANT_TO_OWN 仍缺 target_buy_price → scanner 会静默跳过。"
            " 用 --target <price> 设置。"
        )


if __name__ == "__main__":
    app()
