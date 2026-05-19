"""Perceptual-hash based frame dedupe + per-window click cooldown.

The capture daemon calls ``Deduper.should_keep(image, trigger, window_key)``
on every candidate screenshot. We drop frames whose pHash is within a
trigger-specific Hamming-distance threshold of the last kept frame, plus
collapse rapid-fire clicks in the same window.

Click cooldown: at most one click capture per ``click_cooldown_s`` seconds
per (app, window_title) tuple. Multiple clicks while filling a single form
or scrolling a single document collapse to one "doing something here"
event without losing signal.

Triggers that always pass (no pHash, no cooldown):
- ``window_focus`` — context change is the whole point
- ``paste``, ``save``, ``submit``, ``file_open``, ``file_save``,
  ``url_change`` — deliberate state mutations
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import imagehash
from PIL import Image

from screen_workflow.schemas import TriggerType


_ALWAYS_KEEP = {
    TriggerType.WINDOW_FOCUS,
    TriggerType.PASTE,
    TriggerType.SAVE,
    TriggerType.SUBMIT,
    TriggerType.FILE_OPEN,
    TriggerType.FILE_SAVE,
    TriggerType.URL_CHANGE,
}


@dataclass
class Deduper:
    heartbeat_threshold: int = 5
    click_threshold: int = 8
    click_cooldown_s: float = 2.0
    _last_hash: imagehash.ImageHash | None = field(default=None, init=False)
    _last_click_at_by_window: dict[str, float] = field(default_factory=dict, init=False)

    def reset(self) -> None:
        self._last_hash = None
        self._last_click_at_by_window.clear()

    def should_keep(
        self,
        image: Image.Image,
        trigger: TriggerType,
        window_key: str = "",
    ) -> bool:
        if trigger in _ALWAYS_KEEP:
            self._last_hash = imagehash.phash(image)
            return True

        if trigger is TriggerType.CLICK:
            now = time.monotonic()
            last = self._last_click_at_by_window.get(window_key, -1e9)
            if now - last < self.click_cooldown_s:
                return False
            h = imagehash.phash(image)
            if self._last_hash is not None and (h - self._last_hash) <= self.click_threshold:
                return False
            self._last_click_at_by_window[window_key] = now
            self._last_hash = h
            return True

        # heartbeat
        h = imagehash.phash(image)
        if self._last_hash is None:
            self._last_hash = h
            return True
        if (h - self._last_hash) <= self.heartbeat_threshold:
            return False
        self._last_hash = h
        return True
