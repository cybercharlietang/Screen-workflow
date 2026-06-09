"""Exhaustive tests for the meaningful-event filter."""

from __future__ import annotations

import pytest

from screen_workflow.capture.filter import RawEvent, classify, is_text_input
from screen_workflow.schemas import TriggerType


def E(kind: str, **kw) -> RawEvent:
    return RawEvent(kind=kind, **kw)


class TestKeepers:
    def test_heartbeat(self) -> None:
        r = classify(E("heartbeat"))
        assert r.keep and r.trigger is TriggerType.HEARTBEAT

    def test_window_focus(self) -> None:
        r = classify(E("window_focus", target_label="Outlook"))
        assert r.keep and r.trigger is TriggerType.WINDOW_FOCUS
        assert r.target_label == "Outlook"

    def test_url_change(self) -> None:
        r = classify(E("url_change", target_label="sap.example.com"))
        assert r.keep and r.trigger is TriggerType.URL_CHANGE

    def test_left_click_kept(self) -> None:
        r = classify(E("mouse_click", button="left", is_pressed=True, target_label="Approve"))
        assert r.keep and r.trigger is TriggerType.CLICK
        assert r.target_label == "Approve"

    @pytest.mark.parametrize(
        "key,trigger",
        [
            ("v", TriggerType.PASTE),
            ("s", TriggerType.SAVE),
            ("o", TriggerType.FILE_OPEN),
        ],
    )
    def test_ctrl_chords(self, key: str, trigger: TriggerType) -> None:
        r = classify(E("key", key=key, modifiers=frozenset({"ctrl"})))
        assert r.keep and r.trigger is trigger

    @pytest.mark.parametrize("key", ["enter", "return"])
    def test_submit_keys(self, key: str) -> None:
        r = classify(E("key", key=key))
        assert r.keep and r.trigger is TriggerType.SUBMIT


class TestEnterPrecision:
    def test_enter_while_typing_is_newline_skipped(self) -> None:
        # Enter right after typing characters -> newline, not submit.
        assert not classify(E("key", key="enter"), typing_active=True).keep

    def test_enter_after_pause_is_submit(self) -> None:
        r = classify(E("key", key="enter"), typing_active=False)
        assert r.keep and r.trigger is TriggerType.SUBMIT

    def test_ctrl_enter_submits_even_while_typing(self) -> None:
        # Ctrl+Enter (Outlook/Teams "send") is always a deliberate submit.
        r = classify(
            E("key", key="enter", modifiers=frozenset({"ctrl"})), typing_active=True
        )
        assert r.keep and r.trigger is TriggerType.SUBMIT

    def test_is_text_input_classification(self) -> None:
        assert is_text_input(E("key", key="a")) is True
        assert is_text_input(E("key", key="5")) is True
        assert is_text_input(E("key", key="enter")) is False  # named key, len > 1
        assert is_text_input(E("key", key="v", modifiers=frozenset({"ctrl"}))) is False
        assert is_text_input(E("key", key="a", is_pressed=False)) is False
        assert is_text_input(E("mouse_click", button="left")) is False


class TestSkippers:
    def test_right_click(self) -> None:
        assert not classify(E("mouse_click", button="right", is_pressed=True)).keep

    def test_mouse_release(self) -> None:
        assert not classify(E("mouse_click", button="left", is_pressed=False)).keep

    def test_bare_modifier(self) -> None:
        assert not classify(E("key", key="ctrl")).keep

    def test_plain_letter_key(self) -> None:
        assert not classify(E("key", key="a")).keep

    def test_key_release(self) -> None:
        assert not classify(E("key", key="v", is_pressed=False, modifiers=frozenset({"ctrl"}))).keep

    def test_unknown_event_kind(self) -> None:
        assert not classify(E("scroll")).keep

    def test_cmd_v_also_paste_on_mac(self) -> None:
        # cmd is the macOS modifier; treat as ctrl for chord recognition.
        r = classify(E("key", key="v", modifiers=frozenset({"cmd"})))
        assert r.keep and r.trigger is TriggerType.PASTE
