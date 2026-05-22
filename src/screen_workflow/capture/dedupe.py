"""Perceptual-hash based frame dedupe + per-window click cooldown.

The capture daemon calls ``Deduper.should_keep(image, trigger, window_key)``
on every candidate screenshot. We drop frames whose pHash is within a
trigger-specific Hamming-distance threshold of the last kept frame, plus
collapse rapid-fire clicks in the same window.

Click cooldown: at most one click capture per ``click_cooldown_s`` seconds
per (app, window_title) tuple. Multiple clicks while filling a single form
or scrolling a single document collapse to one "doing something here"
event without losing signal.

Trigger handling:
- ``window_focus``, ``url_change`` — context markers, always pass (a context
  change is the whole point; they are cheap and semantically load-bearing).
- ``paste``, ``save``, ``submit``, ``file_open``, ``file_save`` — deliberate
  state mutations. Kept, but a *tight* pHash check drops a repeated mutation
  on a visually-unchanged screen (e.g. hammering Ctrl+S), which carries no
  new signal.
- ``click`` — per-window cooldown + pHash.
- ``heartbeat`` — pHash only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import imagehash
from PIL import Image

from screen_workflow.schemas import TriggerType


# Context markers — always kept, no pHash, no cooldown.
_CONTEXT_KEEP = {
    TriggerType.WINDOW_FOCUS,
    TriggerType.URL_CHANGE,
}

# Deliberate state mutations — kept unless a tight pHash check says the screen
# is visually unchanged from the last kept frame (a redundant repeated action).
_MUTATION_KEEP = {
    TriggerType.PASTE,
    TriggerType.SAVE,
    TriggerType.SUBMIT,
    TriggerType.FILE_OPEN,
    TriggerType.FILE_SAVE,
}


@dataclass
class Deduper:
    heartbeat_threshold: int = 5
    click_threshold: int = 8
    # Mutation triggers use a deliberately tight threshold: only a near-pixel-
    # identical repeat is dropped, so two genuinely distinct saves both survive.
    mutation_threshold: int = 3
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
        if trigger in _CONTEXT_KEEP:
            self._last_hash = imagehash.phash(image)
            return True

        if trigger in _MUTATION_KEEP:
            h = imagehash.phash(image)
            if (
                self._last_hash is not None
                and (h - self._last_hash) <= self.mutation_threshold
            ):
                return False  # repeated mutation on a visually-unchanged screen
            self._last_hash = h
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
