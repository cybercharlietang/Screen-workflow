"""pHash dedupe behavior."""

from __future__ import annotations

import random

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


def test_first_heartbeat_always_kept() -> None:
    d = Deduper()
    assert d.should_keep(_solid((10, 10, 10)), TriggerType.HEARTBEAT) is True


def test_identical_heartbeat_dropped() -> None:
    d = Deduper()
    d.should_keep(_solid((100, 100, 100)), TriggerType.HEARTBEAT)
    assert d.should_keep(_solid((100, 100, 100)), TriggerType.HEARTBEAT) is False


def test_dissimilar_heartbeat_kept() -> None:
    d = Deduper(threshold=5)
    d.should_keep(_noisy(1), TriggerType.HEARTBEAT)
    assert d.should_keep(_noisy(99), TriggerType.HEARTBEAT) is True


def test_non_heartbeat_trigger_always_kept_even_if_identical() -> None:
    d = Deduper()
    d.should_keep(_solid((50, 50, 50)), TriggerType.CLICK)
    # identical pixels, but click is a deliberate user action
    assert d.should_keep(_solid((50, 50, 50)), TriggerType.CLICK) is True
