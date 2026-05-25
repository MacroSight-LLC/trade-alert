"""Discord embed and chart formatting for trade-alert alerts."""

from formatter.chart import generate_chart
from formatter.embed import compute_rr, format_embed
from formatter.playbook_formatter import PlaybookFormatter

__all__ = [
    "PlaybookFormatter",
    "compute_rr",
    "format_embed",
    "generate_chart",
]
