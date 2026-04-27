"""Recommendation feedback-loop analyzer.

Question it answers: of the Top-N recommendations surfaced to the user that
they did NOT take (``taken=False``) and that have since expired, what fraction
would have been profitable if they had been opened and held to expiration?

Model (single short leg, held to expiration — no rolls, no BTC):

  CC (right="C"):
    kept_premium = avg_open_price * 100   (per contract)
    if close_at_expiry <= strike:  pnl = kept_premium                     (OOM — keep all)
    else:                          pnl = kept_premium - (close - strike) * 100

  CSP (right="P"):
    if close_at_expiry >= strike:  pnl = kept_premium                     (OOM — keep all)
    else:                          pnl = kept_premium - (strike - close) * 100

A recommendation is "profitable" iff pnl > 0. At close == strike the option
pins and kept_premium stays — treat as profitable.

Intentional simplifications:

  * No assignment-follow-through on CC losses. If the stock ran from 50 → 65
    against a $60 call with $1 premium, we report a $400 loss on the option
    leg alone. The underlying shares (which CC holders own) appreciated in
    parallel — we don't net that in because the analyzer is about option
    selection skill, not total-portfolio return.
  * Commission ignored. At IB retail rates (<$1/contract) it's below the
    noise floor for the per-recommendation premium range.
  * Delta / IV at signal time are carried through unchanged for bucketing.

Pure function: consumes dataclass rows + a close-price lookup, returns an
``AnalysisReport``. No DB, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

CONTRACT_MULTIPLIER = 100


@dataclass(frozen=True, slots=True)
class RecRow:
    """Minimal slice of a Recommendation row — narrower than the ORM model."""

    id: int
    symbol: str
    intent: str
    right: str            # "C" | "P"
    strike: float
    expiry: date
    premium: float        # per share, the avg_open_price proxy at recommendation time
    delta: float | None
    dte: int              # original DTE at recommendation time
    annualized_roc: float
    rank: int
    taken: bool


@dataclass(frozen=True, slots=True)
class OutcomeRow:
    rec_id: int
    symbol: str
    intent: str
    right: str
    strike: float
    expiry: date
    premium: float
    close_at_expiry: float
    kept_premium_total: float   # premium * 100 (single contract — reported per-contract)
    pnl_total: float            # per-contract P&L
    profitable: bool            # pnl_total > 0
    rank: int
    taken: bool


@dataclass(frozen=True, slots=True)
class BucketStat:
    label: str
    count: int
    hit_rate: float              # fraction with pnl > 0
    total_pnl: float             # sum of pnl_total across rows (per-contract basis)
    avg_pnl: float               # total_pnl / count
    avg_premium: float           # sum(premium*100)/count — "max you could have made"


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    total_recs: int              # all recs considered (before filtering)
    expired_recs: int            # recs with expiry < as_of
    priced_recs: int             # expired AND close_at_expiry known
    missing_prices: list[tuple[str, date]]  # (symbol, expiry) we couldn't price
    outcomes: list[OutcomeRow]
    by_intent: list[BucketStat]
    by_rank: list[BucketStat]    # rank 1, 2, 3, ...
    by_symbol: list[BucketStat]
    by_taken: list[BucketStat]   # ["taken=False", "taken=True"]
    overall: BucketStat


def classify_outcome(rec: RecRow, close_at_expiry: float) -> OutcomeRow:
    """Compute the single-contract outcome for one rec at a known close."""
    kept_premium_total = rec.premium * CONTRACT_MULTIPLIER
    if rec.right == "C":
        if close_at_expiry <= rec.strike:
            pnl = kept_premium_total
        else:
            pnl = kept_premium_total - (close_at_expiry - rec.strike) * CONTRACT_MULTIPLIER
    elif rec.right == "P":
        if close_at_expiry >= rec.strike:
            pnl = kept_premium_total
        else:
            pnl = kept_premium_total - (rec.strike - close_at_expiry) * CONTRACT_MULTIPLIER
    else:
        raise ValueError(f"Unknown right {rec.right!r} (expected 'C' or 'P')")
    return OutcomeRow(
        rec_id=rec.id,
        symbol=rec.symbol,
        intent=rec.intent,
        right=rec.right,
        strike=rec.strike,
        expiry=rec.expiry,
        premium=rec.premium,
        close_at_expiry=close_at_expiry,
        kept_premium_total=kept_premium_total,
        pnl_total=pnl,
        profitable=pnl > 0,
        rank=rec.rank,
        taken=rec.taken,
    )


def _bucket(label: str, rows: list[OutcomeRow]) -> BucketStat:
    if not rows:
        return BucketStat(label=label, count=0, hit_rate=0.0, total_pnl=0.0,
                          avg_pnl=0.0, avg_premium=0.0)
    total_pnl = sum(r.pnl_total for r in rows)
    hits = sum(1 for r in rows if r.profitable)
    total_premium = sum(r.kept_premium_total for r in rows)
    n = len(rows)
    return BucketStat(
        label=label,
        count=n,
        hit_rate=hits / n,
        total_pnl=total_pnl,
        avg_pnl=total_pnl / n,
        avg_premium=total_premium / n,
    )


def analyze(
    recs: list[RecRow],
    closes: dict[tuple[str, date], float],
    *,
    as_of: date,
    only_not_taken: bool = False,
) -> AnalysisReport:
    """Classify + summarize recs whose expiry has passed.

    ``closes`` keyed by (symbol, expiry_date) → close price. If a rec's
    (symbol, expiry) isn't in the map, it's counted as "missing" and skipped.
    ``only_not_taken`` filters to the feedback-loop question specifically
    ("the ones I skipped — was I right to skip?"); leave False to see both
    sides.
    """
    total_recs = len(recs)
    expired = [r for r in recs if r.expiry < as_of]
    if only_not_taken:
        expired = [r for r in expired if not r.taken]

    outcomes: list[OutcomeRow] = []
    missing: list[tuple[str, date]] = []
    for r in expired:
        close = closes.get((r.symbol, r.expiry))
        if close is None:
            missing.append((r.symbol, r.expiry))
            continue
        outcomes.append(classify_outcome(r, close))

    # Bucket by intent
    by_intent_map: dict[str, list[OutcomeRow]] = {}
    for o in outcomes:
        by_intent_map.setdefault(o.intent, []).append(o)
    by_intent = sorted(
        (_bucket(k, v) for k, v in by_intent_map.items()),
        key=lambda b: b.label,
    )

    # Bucket by rank (1, 2, 3 ...)
    by_rank_map: dict[int, list[OutcomeRow]] = {}
    for o in outcomes:
        by_rank_map.setdefault(o.rank, []).append(o)
    by_rank = [
        _bucket(f"rank {k}", by_rank_map[k])
        for k in sorted(by_rank_map)
    ]

    # Bucket by symbol
    by_symbol_map: dict[str, list[OutcomeRow]] = {}
    for o in outcomes:
        by_symbol_map.setdefault(o.symbol, []).append(o)
    by_symbol = sorted(
        (_bucket(k, v) for k, v in by_symbol_map.items()),
        key=lambda b: b.total_pnl,
        reverse=True,
    )

    # Bucket by taken flag
    taken_rows = [o for o in outcomes if o.taken]
    skipped_rows = [o for o in outcomes if not o.taken]
    by_taken = [
        _bucket("taken=True (opened)", taken_rows),
        _bucket("taken=False (skipped)", skipped_rows),
    ]

    overall = _bucket("overall", outcomes)

    return AnalysisReport(
        total_recs=total_recs,
        expired_recs=len(expired),
        priced_recs=len(outcomes),
        missing_prices=sorted(set(missing)),
        outcomes=outcomes,
        by_intent=by_intent,
        by_rank=by_rank,
        by_symbol=by_symbol,
        by_taken=by_taken,
        overall=overall,
    )
