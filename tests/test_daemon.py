"""Daemon-level pure helpers (no real display / OS hooks)."""

from __future__ import annotations

from screen_workflow.capture.daemon import new_windows


class TestNewWindows:
    def test_detects_newly_opened(self) -> None:
        prev = {"100", "200"}
        cur = {"100", "200", "300"}  # 300 just appeared (a dialog)
        assert new_windows(prev, cur) == {"300"}

    def test_no_change_yields_nothing(self) -> None:
        s = {"100", "200"}
        assert new_windows(s, s) == set()

    def test_closing_a_window_is_not_an_open(self) -> None:
        assert new_windows({"100", "200"}, {"100"}) == set()

    def test_multiple_new_windows(self) -> None:
        assert new_windows({"1"}, {"1", "2", "3"}) == {"2", "3"}
