"""Frame dedupe + per-window click cooldown, with a switchable hash mode.

The capture daemon calls ``Deduper.should_keep(image, trigger, window_key)``
on every candidate screenshot and drops frames that carry no new signal.

Hash mode (``hash_mode``) — the fidelity/cost knob:

- ``"perceptual"`` (default): a perceptual hash (pHash). Cheap-ish to compute,
  and it drops *near*-duplicates — frames that look the same to a 32x32
  grayscale DCT even if a few pixels differ (taskbar clock, cursor, a flapping
  title-bar counter). Big frame-count (and therefore API-cost) savings, but it
  is **not exact**: a small change pHash can't see (a digit in a cell, a
  red->green status of the same shape) can be dropped. Tuned by Hamming-distance
  thresholds (out of 64 bits).
- ``"exact"``: a cryptographic hash of the raw pixels. Drops a frame only if it
  is **pixel-identical** to a recent kept frame. Never wrongly drops a real
  change, but keeps far more frames (almost nothing is ever pixel-identical),
  so it costs much more downstream. Use it to measure the trade-off.

Both modes share the same structure: each candidate is compared against the
last ``recent_window`` kept frames (a ring buffer — kills the A->B->A flip
where the returning frame is a near-duplicate of one two-back).

``stats()`` exposes seen/kept/dropped counts and average hash time so the two
modes can be compared on a real run.
"""

from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import imagehash
from PIL import Image

from screen_workflow.schemas import TriggerType


HASH_MODE_PERCEPTUAL = "perceptual"
HASH_MODE_EXACT = "exact"
HASH_MODES = (HASH_MODE_PERCEPTUAL, HASH_MODE_EXACT)


# Context markers — a true context shift is always signal.
_CONTEXT_KEEP = {
    TriggerType.WINDOW_FOCUS,
    TriggerType.URL_CHANGE,
}

# Deliberate state mutations — kept unless a tight check says the screen is
# visually unchanged from the last kept frame (a redundant repeated action).
_MUTATION_KEEP = {
    TriggerType.PASTE,
    TriggerType.SAVE,
    TriggerType.SUBMIT,
    TriggerType.FILE_OPEN,
    TriggerType.FILE_SAVE,
}

# Separator the daemon uses to build window_key = f"{app}<sep>{title}".
_WINDOW_KEY_SEP = "␟"

_EXACT_FAR = 1_000  # "distance" returned for non-identical exact fingerprints


@dataclass
class Deduper:
    hash_mode: str = HASH_MODE_PERCEPTUAL
    heartbeat_threshold: int = 5
    click_threshold: int = 8
    # Mutation triggers use a deliberately tight threshold: only a near-pixel-
    # identical repeat is dropped, so two genuinely distinct saves both survive.
    mutation_threshold: int = 3
    # Title-only window_focus flap is dropped when within this of a recent frame.
    context_threshold: int = 5
    click_cooldown_s: float = 2.0
    # How many recently-kept frame fingerprints to dedupe against.
    recent_window: int = 8
    _recent: deque = field(default_factory=deque, init=False)
    _last_click_at_by_window: dict[str, float] = field(default_factory=dict, init=False)
    _last_app: str | None = field(default=None, init=False)
    # instrumentation
    _seen: int = field(default=0, init=False)
    _kept: int = field(default=0, init=False)
    _hash_seconds: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if self.hash_mode not in HASH_MODES:
            raise ValueError(f"hash_mode must be one of {HASH_MODES}, got {self.hash_mode!r}")
        self._recent = deque(maxlen=self.recent_window)

    def reset(self) -> None:
        self._recent.clear()
        self._last_click_at_by_window.clear()
        self._last_app = None

    def stats(self) -> dict[str, Any]:
        """Counts + timing for comparing hash modes on a real run."""
        return {
            "mode": self.hash_mode,
            "seen": self._seen,
            "kept": self._kept,
            "dropped": self._seen - self._kept,
            "keep_rate": (self._kept / self._seen) if self._seen else 0.0,
            "avg_hash_ms": (self._hash_seconds / self._seen * 1000.0) if self._seen else 0.0,
        }

    # -- fingerprint + distance (mode-dependent) ---------------------------

    def _fingerprint(self, image: Image.Image) -> Any:
        t = time.perf_counter()
        if self.hash_mode == HASH_MODE_EXACT:
            fp: Any = hashlib.blake2b(image.tobytes(), digest_size=16).digest()
        else:
            fp = imagehash.phash(image)
        self._hash_seconds += time.perf_counter() - t
        return fp

    def _distance(self, a: Any, b: Any) -> int:
        if self.hash_mode == HASH_MODE_EXACT:
            return 0 if a == b else _EXACT_FAR
        return a - b  # pHash Hamming distance

    def _is_near(self, fp: Any, threshold: int) -> bool:
        """True if ``fp`` is within ``threshold`` of any recently-kept frame."""
        return any(self._distance(fp, prev) <= threshold for prev in self._recent)

    def _remember(self, fp: Any) -> None:
        self._recent.append(fp)

    # -- main entry point --------------------------------------------------

    def should_keep(
        self,
        image: Image.Image,
        trigger: TriggerType,
        window_key: str = "",
    ) -> bool:
        self._seen += 1
        keep = self._decide(image, trigger, window_key)
        if keep:
            self._kept += 1
        return keep

    def _decide(
        self,
        image: Image.Image,
        trigger: TriggerType,
        window_key: str,
    ) -> bool:
        # A new window / dialog is a decision point — always keep it. Dropping
        # one (a false negative) is far costlier than an extra frame.
        if trigger is TriggerType.WINDOW_OPEN:
            self._remember(self._fingerprint(image))
            return True

        if trigger in _CONTEXT_KEEP:
            return self._keep_context(image, trigger, window_key)

        if trigger in _MUTATION_KEEP:
            fp = self._fingerprint(image)
            # Compare only to the most recent kept frame: two distinct deliberate
            # actions should both survive even if an older frame looked similar.
            if self._recent and self._distance(fp, self._recent[-1]) <= self.mutation_threshold:
                return False
            self._remember(fp)
            return True

        if trigger is TriggerType.CLICK:
            now = time.monotonic()
            last = self._last_click_at_by_window.get(window_key, -1e9)
            if now - last < self.click_cooldown_s:
                return False
            fp = self._fingerprint(image)
            if self._is_near(fp, self.click_threshold):
                return False
            self._last_click_at_by_window[window_key] = now
            self._remember(fp)
            return True

        # heartbeat
        fp = self._fingerprint(image)
        if self._is_near(fp, self.heartbeat_threshold):
            return False
        self._remember(fp)
        return True

    # -- context frames ----------------------------------------------------

    def _keep_context(
        self,
        image: Image.Image,
        trigger: TriggerType,
        window_key: str,
    ) -> bool:
        fp = self._fingerprint(image)
        app = window_key.split(_WINDOW_KEY_SEP)[0]
        app_changed = app != self._last_app
        self._last_app = app

        # A real context shift (app switch) or a URL navigation is always signal.
        if trigger is TriggerType.URL_CHANGE or app_changed:
            self._remember(fp)
            return True

        # Same app, title-only change: drop pure visual flap (unread counters,
        # progress %, clocks in the title bar) that carries no new screen state.
        # NB: in exact mode the title-bar text is on-screen, so the fingerprint
        # differs and the frame is kept — only pHash blurs the title away.
        if self._is_near(fp, self.context_threshold):
            return False
        self._remember(fp)
        return True
