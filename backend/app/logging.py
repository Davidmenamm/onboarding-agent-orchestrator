import logging

import structlog

from app.pii import mask_log_value


def _mask(_, __, event_dict):
    # Defense in depth: mask long digit runs (IDs) in every log value.
    return {k: mask_log_value(v) if isinstance(v, str) else v for k, v in event_dict.items()}


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _mask,
            structlog.processors.JSONRenderer(),
        ]
    )
