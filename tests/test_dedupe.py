"""Dedupe and click-cooldown behavior."""

from __future__ import annotations

import random
import time

from PIL import Image

from screen_workflow.capture.dedupe import Deduper
from screen_workflow.schemas import TriggerType


def _solid(color: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", (64, 64), color=color)


def _noisy(seed: int) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("RGB", (64, 64))
    px = img.load()
    for x in range(64):
        for y in range(64):
            px[x, y] = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
    return img


class TestHeartbeat:
    def test_first_heartbeat_always_kept(self) -> None:
        d = Deduper()
        assert d.should_keep(_solid((10, 10, 10)), TriggerType.HEARTBEAT) is True

    def test_identical_heartbeat_dropped(self) -> None:
        d = Deduper()
        d.should_keep(_solid((100, 100, 100)), TriggerType.HEARTBEAT)
        assert d.should_keep(_solid((100, 100, 100)), TriggerType.HEARTBEAT) is False

    def test_dissimilar_heartbeat_kept(self) -> None:
        d = Deduper(heartbeat_threshold=5)
        d.should_keep(_noisy(1), TriggerType.HEARTBEAT)
        assert d.should_keep(_noisy(99), TriggerType.HEARTBEAT) is True


class TestAlwaysKeep:
    def test_window_focus_always_kept(self) -> None:
        d = Deduper()
        d.should_keep(_solid((50, 50, 50)), TriggerType.WINDOW_FOCUS, "w1")
        assert d.should_keep(_solid((50, 50, 50)), TriggerType.WINDOW_FOCUS, "w1") is True

    def test_submit_always_kept(self) -> None:
        d = Deduper()
        assert d.should_keep(_solid((50, 50, 50)), TriggerType.SUBMIT, "w1") is True


class TestMutationDedup:
    def test_first_save_kept(self) -> None:
        d = Deduper()
        assert d.should_keep(_solid((30, 30, 30)), TriggerType.SAVE) is True

    def test_repeated_save_on_unchanged_screen_dropped(self) -> None:
        d = Deduper()
        d.should_keep(_solid((30, 30, 30)), TriggerType.SAVE)
        # Identical screen — a second save carries no new signal.
        assert d.should_keep(_solid((30, 30, 30)), TriggerType.SAVE) is False

    def test_save_after_screen_change_kept(self) -> None:
        d = Deduper()
        d.should_keep(_noisy(1), TriggerType.SAVE)
        # Screen changed substantially — the second save is real signal.
        assert d.should_keep(_noisy(42), TriggerType.SAVE) is True

    def test_mutation_dedupes_against_any_prior_kept_frame(self) -> None:
        d = Deduper()
        d.should_keep(_solid((80, 80, 80)), TriggerType.HEARTBEAT)
        # Paste onto a screen identical to the last kept frame — dropped.
        assert d.should_keep(_solid((80, 80, 80)), TriggerType.PASTE) is False


class TestClick:
    def test_first_click_kept(self) -> None:
        d = Deduper()
        assert d.should_keep(_solid((10, 10, 10)), TriggerType.CLICK, "Outlook|Inbox") is True

    def test_rapid_second_click_in_same_window_dropped(self) -> None:
        d = Deduper(click_cooldown_s=2.0)
        d.should_keep(_solid((10, 10, 10)), TriggerType.CLICK, "Outlook|Inbox")
        assert d.should_keep(_noisy(1), TriggerType.CLICK, "Outlook|Inbox") is False

    def test_rapid_click_in_different_window_kept(self) -> None:
        d = Deduper(click_cooldown_s=2.0)
        d.should_keep(_solid((10, 10, 10)), TriggerType.CLICK, "Outlook|Inbox")
        # different window key, no cooldown applies
        assert d.should_keep(_noisy(7), TriggerType.CLICK, "Chrome|SAP") is True

    def test_click_after_cooldown_kept(self) -> None:
        d = Deduper(click_cooldown_s=0.05)
        d.should_keep(_noisy(1), TriggerType.CLICK, "w1")
        time.sleep(0.08)
        assert d.should_keep(_noisy(99), TriggerType.CLICK, "w1") is True

    def test_click_with_identical_pixels_dropped(self) -> None:
        d = Deduper(click_cooldown_s=0.0, click_threshold=8)
        d.should_keep(_solid((100, 100, 100)), TriggerType.CLICK, "w1")
        assert d.should_keep(_solid((100, 100, 100)), TriggerType.CLICK, "w1") is False
