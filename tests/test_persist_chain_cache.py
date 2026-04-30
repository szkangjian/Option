"""``_persist_chain_cache`` regression: don't clobber prior OI / volume with None.

The streaming OI pass occasionally misses (rate-limit, slow tick), so when
a fresh fetch returns None we want the previous good value to remain — not
get nuked. Same rule applies to volume.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from options_tool.advisor import _persist_chain_cache
from options_tool.db import ChainCache, init_db, session_scope
from options_tool.ibkr import OptionQuote


def _fresh_db(tmp_path, monkeypatch):
    from options_tool import db as db_mod, settings as settings_mod

    db_path = tmp_path / "t.db"
    monkeypatch.setenv("OPTIONS_TOOL_DB_PATH", str(db_path))
    settings_mod.get_settings.cache_clear()
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db()


def _quote(strike: float, *, oi: int | None, vol: int | None,
           bid: float = 1.0, ask: float = 1.2) -> OptionQuote:
    return OptionQuote(
        symbol="TST",
        expiry=date(2026, 6, 19),
        strike=strike,
        right="P",
        bid=bid,
        ask=ask,
        last=None,
        delta=-0.20,
        gamma=None, theta=None, vega=None, iv=None,
        open_interest=oi,
        volume=vol,
        underlying_price=100.0,
    )


def test_oi_persisted_on_first_write(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    _persist_chain_cache([_quote(50, oi=842, vol=12)])
    with session_scope() as s:
        row = s.execute(
            select(ChainCache.open_interest, ChainCache.volume)
            .where(ChainCache.strike == 50)
        ).first()
    assert row.open_interest == 842
    assert row.volume == 12


def test_none_oi_does_not_clobber_existing(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    # Seed a row with a known good OI.
    _persist_chain_cache([_quote(50, oi=842, vol=12, bid=1.0, ask=1.2)])
    # Second fetch comes in with OI=None (streaming missed the tick).
    _persist_chain_cache([_quote(50, oi=None, vol=None, bid=1.1, ask=1.3)])
    with session_scope() as s:
        row = s.execute(
            select(ChainCache.bid, ChainCache.ask,
                   ChainCache.open_interest, ChainCache.volume)
            .where(ChainCache.strike == 50)
        ).first()
    # Bid/ask updated; OI/volume preserved.
    assert row.bid == 1.1
    assert row.ask == 1.3
    assert row.open_interest == 842
    assert row.volume == 12


def test_oi_overwritten_by_fresh_value(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    # The no-clobber rule must NOT block legitimate updates.
    _persist_chain_cache([_quote(50, oi=100, vol=5)])
    _persist_chain_cache([_quote(50, oi=999, vol=42)])
    with session_scope() as s:
        row = s.execute(
            select(ChainCache.open_interest, ChainCache.volume)
            .where(ChainCache.strike == 50)
        ).first()
    assert row.open_interest == 999
    assert row.volume == 42
