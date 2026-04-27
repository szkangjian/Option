"""Centralized logging configuration.

ib_async logs every position, portfolio update, and warning at INFO. That is
useful when debugging the adapter but overwhelming for normal CLI use. We set
ib_async loggers to WARNING by default and let the user opt in to verbose.
"""
from __future__ import annotations

import logging


def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    if not verbose:
        # Silence the worst offenders. Wrapper logs every position, fill,
        # portfolio update at INFO; ib logs invalid contracts at WARNING
        # (which we expect during chain pulls because IB's secDef returns
        # strike unions across expiries that aren't all listed).
        for noisy in ("ib_async", "ib_async.wrapper", "ib_async.client", "ib_async.ib"):
            logging.getLogger(noisy).setLevel(logging.ERROR)
        logging.getLogger("ib_async.wrapper").addFilter(_drop_unknown_contract)

    # ALWAYS silence httpx's request log — Telegram puts the bot token in the
    # URL path, and httpx logs the full URL at INFO. Even verbose mode keeps
    # this off; nothing we'd want to debug lives in that line.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _drop_unknown_contract(record: logging.LogRecord) -> bool:
    """Filter out the 'Error 200' / 'Unknown contract' chatter."""
    msg = record.getMessage()
    if "Error 200" in msg or "Unknown contract" in msg:
        return False
    return True
