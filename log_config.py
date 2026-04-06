"""Centralized structured logging configuration.

Import ``configure_logging()`` at the top of each entry-point module
(pipeline_runner, notifier_and_logger, discord_bot, dashboard_api,
scripts/mcp_server) to switch from unstructured text to JSON logs.

Toggle via ``LOG_FORMAT`` env var: ``json`` (default) or ``text``.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog

_CONFIGURED = False


def configure_logging(*, default_level: int = logging.INFO) -> None:
    """Set up structlog JSON (or text) logging, only once per process.

    Args:
        default_level: Root logging level.
    """
    global _CONFIGURED  # noqa: PLW0603
    if _CONFIGURED:
        return
    _CONFIGURED = True

    log_format = os.getenv("LOG_FORMAT", "json").lower()
    use_json = log_format != "text"

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if use_json:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(default_level)

    # Quiet noisy third-party loggers
    for name in ("httpx", "httpcore", "uvicorn.access", "litellm"):
        logging.getLogger(name).setLevel(logging.WARNING)
