"""Perceptual-hash based frame dedupe + per-window click cooldown.

The capture daemon calls ``Deduper.should_keep(image, trigger, window_key)``
on every candidate screenshot. We drop frames whose pHash is within a
trigger-specific Hamming-distance threshold of any *recently kept* frame, plus
collapse rapid-fire clicks in the same window.

Recent-frame buffer: dedupe compares each candidate against the last
``recent_window`` kept frames, not just the immediately-previous one. This
kills the A->B->A pattern (flip to Outlook, to Chrome, back to Outlook) where
the third frame is a near-duplicate of the first but a single-frame check would
keep it.

Click cooldown: at most one click capture per ``click_cooldown_s`` seconds
per (app, window_title) tuple. Multiple clicks while filling a single form
or scrolling a single document collapse to one "doing something here"
event without losing signal.

Trigger handling:
- ``window_focus`` — kept unconditionally when the *app* changed (a real
  context shift), but a *title-only* change on the same app (unread counters,
  download %, clocks, "Saving..." in the title bar) is pHash-gated so it
  doesn't leak a frame every second.
- ``url_change`` — a navigation; always kept.
- ``paste``, ``save``, ``submit``, ``file_open``, ``file_save`` — deliberate
  state mutations. Kept, but a *tight* pHash check against the last kept frame
  drops a repeated mutation on a visually-unchanged screen (e.g. hammering
  Ctrl+S), which carries no new signal.
- ``click`` — per-window cooldown + pHash against the recent buffer.
- ``heartbeat`` — pHash against the recent buffer.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

import imagehash
from PIL import Image

from screen_workflow.schemas import TriggerType


# Context markers — a true context shift is always signal.
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

# Separator the daemon uses to build window_key = f"{app}<sep>{title}".
_WINDOW_KEY_SEP = "␟"


@dataclass
class Deduper:
    heartbeat_threshold: int = 5
    click_threshold: int = 8
    # Mutation triggers use a deliberately tight threshold: only a near-pixel-
    # identical repeat is dropped, so two genuinely distinct saves both survive.
    mutation_threshold: int = 3
    # Title-only window_focus flap is dropped when within this of a recent frame.
    context_threshold: int = 5
    click_cooldown_s: float = 2.0
    # How many recently-kept frame hashes to dedupe against.
    recent_window: int = 8
    _recent: deque = field(default_factory=deque, init=False)
    _last_click_at_by_window: dict[str, float] = field(default_factory=dict, init=False)
    _last_app: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._recent = deque(maxlen=self.recent_window)

    def reset(self) -> None:
        self._recent.clear()
        self._last_click_at_by_window.clear()
        self._last_app = None

    # -- helpers -----------------------------------------------------------

    def _is_near(self, h: imagehash.ImageHash, threshold: int) -> bool:
        """True if ``h`` is within ``threshold`` of any recently-kept frame."""
        return any((h - prev) <= threshold for prev in self._recent)

    def _remember(self, h: imagehash.ImageHash) -> None:
        self._recent.append(h)

    # -- main entry point --------------------------------------------------

    def should_keep(
        self,
        image: Image.Image,
        trigger: TriggerType,
        window_key: str = "",
    ) -> bool:
        if trigger in _CONTEXT_KEEP:
            return self._keep_context(image, trigger, window_key)

        if trigger in _MUTATION_KEEP:
            h = imagehash.phash(image)
            # Compare only to the most recent kept frame: two distinct deliberate
            # actions should both survive even if an older frame looked similar.
            if self._recent and (h - self._recent[-1]) <= self.mutation_threshold:
                return False
            self._remember(h)
            return True

        if trigger is TriggerType.CLICK:
            now = time.monotonic()
            last = self._last_click_at_by_window.get(window_key, -1e9)
            if now - last < self.click_cooldown_s:
                return False
            h = imagehash.phash(image)
            if self._is_near(h, self.click_threshold):
                return False
            self._last_click_at_by_window[window_key] = now
            self._remember(h)
            return True

        # heartbeat
        h = imagehash.phash(image)
        if self._is_near(h, self.heartbeat_threshold):
            return False
        self._remember(h)
        return True

    # -- context frames ----------------------------------------------------

    def _keep_context(
        self,
        image: Image.Image,
        trigger: TriggerType,
        window_key: str,
    ) -> bool:
        h = imagehash.phash(image)
        app = window_key.split(_WINDOW_KEY_SEP)[0]
        app_changed = app != self._last_app
        self._last_app = app

        # A real context shift (app switch) or a URL navigation is always signal.
        if trigger is TriggerType.URL_CHANGE or app_changed:
            self._remember(h)
            return True

        # Same app, title-only change: drop pure visual flap (unread counters,
        # progress %, clocks in the title bar) that carries no new screen state.
        if self._is_near(h, self.context_threshold):
            return False
        self._remember(h)
        return True
