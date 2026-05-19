"""Perceptual-hash based frame dedupe.

The capture daemon calls ``Deduper.should_keep(image)`` on every candidate
screenshot. We drop frames whose pHash is within ``threshold`` Hamming
distance of the last kept frame — typically the difference between two
identical-looking screens. Trigger type ``HEARTBEAT`` is the main source of
duplicates; clicks always pass through because they almost always coincide
with at least small pixel changes.

For PoC we just compare against the last kept frame (not a window). Cheap,
fast, prevents the obvious 30-frames-of-the-same-static-window waste.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import imagehash
from PIL import Image

from screen_workflow.schemas import TriggerType


@dataclass
class Deduper:
    threshold: int = 5
    _last_hash: imagehash.ImageHash | None = field(default=None, init=False)

    def reset(self) -> None:
        self._last_hash = None

    def should_keep(self, image: Image.Image, trigger: TriggerType) -> bool:
        # Non-heartbeat triggers always pass — user did something deliberate.
        if trigger is not TriggerType.HEARTBEAT:
            h = imagehash.phash(image)
            self._last_hash = h
            return True

        h = imagehash.phash(image)
        if self._last_hash is None:
            self._last_hash = h
            return True
        dist = h - self._last_hash
        if dist <= self.threshold:
            return False
        self._last_hash = h
        return True
