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


def is_text_input(event: RawEvent) -> bool:
    """True if this event is a single printable character being typed.

    The daemon uses this to track 'typing flow' so it can tell a deliberate
    Enter (submit) from an Enter that's just a newline mid-sentence. Modifier
    chords (Ctrl+V etc.) and named keys (enter, backspace, tab) are not text.
    """
    return (
        event.kind == "key"
        and event.is_pressed
        and event.key is not None
        and len(event.key) == 1
        and not (event.modifiers & {"ctrl", "cmd"})
    )


def classify(event: RawEvent, *, typing_active: bool = False) -> FilterResult:
    """Decide whether the capture daemon should grab a screenshot.

    ``typing_active`` is set by the daemon when a printable character was typed
    in the last second; it down-weights a bare Enter from SUBMIT to a newline.
    """
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
            # Wired end-to-end (filter + dedupe) but NOT currently emitted: no
            # event source produces "url_change" yet. Browser navigations are
            # only caught when the window *title* changes (window_focus). True
            # in-page (SPA) URL changes are missed until we add a browser hook
            # — see TODOS.md "Browser extension for URL fidelity".
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
            return _classify_key(event, typing_active=typing_active)
    return SKIP


def _classify_key(event: RawEvent, *, typing_active: bool = False) -> FilterResult:
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
        # Enter mid-typing is a newline, not a submit — skip it. A deliberate
        # submit (search box, form) usually follows a pause or a paste.
        if typing_active and not mods:
            return SKIP
        return FilterResult(
            keep=True,
            trigger=TriggerType.SUBMIT,
            target_label=event.target_label,
        )

    return SKIP
