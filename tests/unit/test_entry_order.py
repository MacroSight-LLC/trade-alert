"""Unit tests for gates.entry_order."""

from __future__ import annotations

import pytest

from gates.entry_order import EntryOrder


class TestEntryOrder:
    def test_long_valid_ordering(self) -> None:
        order = EntryOrder(level=185.0, stop=182.0, target=195.0)
        assert order.has_valid_ordering("LONG")
        assert order.is_valid_for_direction("LONG")

    def test_short_valid_ordering(self) -> None:
        order = EntryOrder(level=185.0, stop=188.0, target=175.0)
        assert order.has_valid_ordering("SHORT")
        assert order.is_valid_for_direction("SHORT")

    def test_watch_skips_ordering(self) -> None:
        order = EntryOrder(level=0.0, stop=0.0, target=0.0)
        assert order.has_valid_ordering("WATCH")
        assert order.is_valid_for_direction("WATCH")

    def test_non_positive_fails_gate_not_schema_ordering(self) -> None:
        order = EntryOrder(level=185.0, stop=190.0, target=195.0)
        assert order.has_valid_ordering("LONG") is False
        assert order.is_valid_for_direction("LONG") is False

    def test_from_dict_missing_key(self) -> None:
        with pytest.raises(ValueError, match="entry missing required keys"):
            EntryOrder.from_dict({"level": 1.0})
