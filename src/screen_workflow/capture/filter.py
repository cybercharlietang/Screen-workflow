"""Pure functions deciding whether a raw OS event warrants a screenshot.

Kept dependency-free and side-effect-free so it can be unit-tested
exhaustively without a real display.
"""

from __future__ import annotations

from dataclasses import dataclass

from screen_workflow.schemas import TriggerType


@dataclass(frozen=True)
class RawEvent:
    """The shape of an OS event after our hook normalizes it."""

    kind: str  # "mouse_click" | "key" | "window_focus" | "url_change" | "heartbeat"
    button: str | None = None  # for mouse_click: "left" | "right" | "middle"
    is_pressed: bool = True
    key: str | None = None  # for key: e.g. "v", "s", "enter"
    modifiers: frozenset[str] = frozenset()  # {"ctrl", "shift", "alt", "cmd"}
    target_label: str | None = None  # accessibility label if known


@dataclass(frozen=True)
class FilterResult:
    """Either keep a frame (with a trigger label) or skip."""

    keep: bool
    trigger: TriggerType | None = None
    target_label: str | None = None


SKIP = FilterResult(keep=False)


def classify(event: RawEvent) -> FilterResult:
    """Decide whether the capture daemon should grab a screenshot."""
    match event.kind:
        case "heartbeat":
            return FilterResult(keep=True, trigger=TriggerType.HEARTBEAT)
        case "window_focus":
            return FilterResult(
                keep=True,
                trigger=TriggerType.WINDOW_FOCUS,
                target_label=event.target_label,
            )
        case "url_change":
            return FilterResult(
                keep=True,
                trigger=TriggerType.URL_CHANGE,
                target_label=event.target_label,
            )
        case "mouse_click":
            # Only left-button press on interactive-looking targets, or any
            # left press if we have no accessibility info.
            if event.button != "left" or not event.is_pressed:
                return SKIP
            return FilterResult(
                keep=True,
                trigger=TriggerType.CLICK,
                target_label=event.target_label,
            )
        case "key":
            return _classify_key(event)
    return SKIP


def _classify_key(event: RawEvent) -> FilterResult:
    if not event.is_pressed or event.key is None:
        return SKIP
    key = event.key.lower()
    mods = {m.lower() for m in event.modifiers}

    # Bare modifiers are never meaningful on their own.
    if key in {"ctrl", "shift", "alt", "cmd", "meta"}:
        return SKIP

    if "ctrl" in mods or "cmd" in mods:
        if key == "v":
            return FilterResult(keep=True, trigger=TriggerType.PASTE)
        if key == "s":
            return FilterResult(keep=True, trigger=TriggerType.SAVE)
        if key == "o":
            return FilterResult(keep=True, trigger=TriggerType.FILE_OPEN)

    if key in {"enter", "return"}:
        return FilterResult(
            keep=True,
            trigger=TriggerType.SUBMIT,
            target_label=event.target_label,
        )

    return SKIP
