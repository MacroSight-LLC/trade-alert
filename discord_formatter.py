"""Backward-compatible shim — canonical code in formatter/embed.py."""

from formatter.embed import (  # noqa: F401
    MAX_EMBED_DESCRIPTION_CHARS,
    MAX_EMBED_FIELDS,
    _enforce_embed_limits,
    _format_watch_embed,
    _quality_color,
    _route_channel_for_alert,
    _score_bar,
    _truncate_field,
    compute_rr,
    format_embed,
)

__all__ = [
    "MAX_EMBED_DESCRIPTION_CHARS",
    "MAX_EMBED_FIELDS",
    "_enforce_embed_limits",
    "_format_watch_embed",
    "_quality_color",
    "_route_channel_for_alert",
    "_score_bar",
    "_truncate_field",
    "compute_rr",
    "format_embed",
]
