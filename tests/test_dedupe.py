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


SEP = "␟"  # window_key separator the daemon uses: f"{app}{SEP}{title}"


class TestContextFrames:
    def test_app_switch_always_kept(self) -> None:
        """Switching apps is a real context shift — kept even if visually similar."""
        d = Deduper()
        d.should_keep(_solid((50, 50, 50)), TriggerType.WINDOW_FOCUS, f"OUTLOOK.EXE{SEP}Inbox")
        assert (
            d.should_keep(_solid((50, 50, 50)), TriggerType.WINDOW_FOCUS, f"CHROME.EXE{SEP}SAP")
            is True
        )

    def test_title_flap_same_app_dropped(self) -> None:
        """Same app, title-only change, unchanged pixels (e.g. unread counter
        Inbox (3) -> Inbox (4)) is pure flap and dropped."""
        d = Deduper()
        d.should_keep(_solid((50, 50, 50)), TriggerType.WINDOW_FOCUS, f"OUTLOOK.EXE{SEP}Inbox (3)")
        assert (
            d.should_keep(_solid((50, 50, 50)), TriggerType.WINDOW_FOCUS, f"OUTLOOK.EXE{SEP}Inbox (4)")
            is False
        )

    def test_title_change_with_visual_change_kept(self) -> None:
        """Same app but the screen actually changed — real signal, kept."""
        d = Deduper()
        d.should_keep(_noisy(1), TriggerType.WINDOW_FOCUS, f"CHROME.EXE{SEP}Page A")
        assert (
            d.should_keep(_noisy(50), TriggerType.WINDOW_FOCUS, f"CHROME.EXE{SEP}Page B") is True
        )

    def test_url_change_always_kept(self) -> None:
        d = Deduper()
        d.should_keep(_solid((50, 50, 50)), TriggerType.URL_CHANGE, f"CHROME.EXE{SEP}A")
        assert (
            d.should_keep(_solid((50, 50, 50)), TriggerType.URL_CHANGE, f"CHROME.EXE{SEP}A") is True
        )

    def test_submit_always_kept(self) -> None:
        d = Deduper()
        assert d.should_keep(_solid((50, 50, 50)), TriggerType.SUBMIT, "w1") is True

    def test_window_open_always_kept_even_if_identical(self) -> None:
        # A dialog is a decision point — never deduped away, asymmetric payoff.
        d = Deduper()
        d.should_keep(_solid((70, 70, 70)), TriggerType.WINDOW_OPEN, "App␟Dialog")
        assert d.should_keep(_solid((70, 70, 70)), TriggerType.WINDOW_OPEN, "App␟Dialog") is True


class TestRingBuffer:
    def test_aba_flip_drops_returning_frame(self) -> None:
        """A->B->A: the returning frame matches a hash 2-back and is dropped,
        where a single-last-frame check would have kept it."""
        d = Deduper(heartbeat_threshold=5)
        a, b = _noisy(1), _noisy(2)
        assert d.should_keep(a, TriggerType.HEARTBEAT) is True
        assert d.should_keep(b, TriggerType.HEARTBEAT) is True
        assert d.should_keep(a, TriggerType.HEARTBEAT) is False  # ring buffer catches it

    def test_frame_older_than_window_is_not_deduped(self) -> None:
        """Once a frame ages out of the recent window, an identical frame is
        kept again (bounded memory)."""
        d = Deduper(heartbeat_threshold=5, recent_window=2)
        a = _noisy(1)
        assert d.should_keep(a, TriggerType.HEARTBEAT) is True
        d.should_keep(_noisy(2), TriggerType.HEARTBEAT)
        d.should_keep(_noisy(3), TriggerType.HEARTBEAT)  # a now evicted (maxlen=2)
        assert d.should_keep(a, TriggerType.HEARTBEAT) is True


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


class TestHashModeExact:
    def test_pixel_identical_dropped(self) -> None:
        d = Deduper(hash_mode="exact")
        d.should_keep(_solid((100, 100, 100)), TriggerType.HEARTBEAT)
        # byte-for-byte identical -> dropped
        assert d.should_keep(_solid((100, 100, 100)), TriggerType.HEARTBEAT) is False

    def test_one_pixel_difference_kept(self) -> None:
        """Exact mode keeps a frame that perceptual hashing would have dropped:
        a tiny change pHash blurs away is a real, distinct frame here."""
        base = _solid((100, 100, 100))
        nudged = base.copy()
        nudged.putpixel((0, 0), (101, 100, 100))  # single-channel, single-pixel
        d_exact = Deduper(hash_mode="exact")
        d_exact.should_keep(base, TriggerType.HEARTBEAT)
        assert d_exact.should_keep(nudged, TriggerType.HEARTBEAT) is True
        # perceptual hashing collapses the same pair
        d_phash = Deduper(hash_mode="perceptual")
        d_phash.should_keep(base, TriggerType.HEARTBEAT)
        assert d_phash.should_keep(nudged, TriggerType.HEARTBEAT) is False

    def test_invalid_mode_rejected(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            Deduper(hash_mode="fuzzy")


class TestStats:
    def test_counts_seen_kept_dropped(self) -> None:
        d = Deduper()
        d.should_keep(_solid((100, 100, 100)), TriggerType.HEARTBEAT)  # kept
        d.should_keep(_solid((100, 100, 100)), TriggerType.HEARTBEAT)  # dropped
        s = d.stats()
        assert s["seen"] == 2
        assert s["kept"] == 1
        assert s["dropped"] == 1
        assert s["keep_rate"] == 0.5
        assert s["mode"] == "perceptual"
        assert s["avg_hash_ms"] >= 0.0


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
