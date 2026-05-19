"""Cross-stage data contracts.

These pydantic models define the interface between every pipeline stage.
Changing them is a breaking change — bump ``SCHEMA_VERSION`` and update
``SPEC.md`` § 4 in the same commit.

See SPEC.md § 4 for how each model is produced and consumed.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Event (Phase 1 output)
# ---------------------------------------------------------------------------


class TriggerType(str, Enum):
    """Why the capture layer decided this frame was worth keeping."""

    WINDOW_FOCUS = "window_focus"
    CLICK = "click"
    PASTE = "paste"
    SAVE = "save"
    SUBMIT = "submit"
    URL_CHANGE = "url_change"
    FILE_OPEN = "file_open"
    FILE_SAVE = "file_save"
    HEARTBEAT = "heartbeat"


class UIElement(BaseModel):
    """One node from the accessibility tree at the active control's level."""

    model_config = ConfigDict(extra="forbid")

    role: str
    label: str | None = None
    bbox: tuple[int, int, int, int] | None = Field(
        default=None,
        description="(left, top, right, bottom) in screen coordinates",
    )


class InputEvent(BaseModel):
    """The raw OS event that triggered this frame, if any."""

    model_config = ConfigDict(extra="forbid")

    type: TriggerType
    target_label: str | None = None


class Event(BaseModel):
    """One kept frame: a screenshot plus the local enrichment we did on it."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    event_id: str = Field(description="ULID; sortable by time")
    ts: datetime
    session_id: str | None = Field(
        default=None,
        description="Assigned by the session segmenter after the event lands",
    )

    app: str = Field(description="Process name, e.g. 'OUTLOOK.EXE'")
    window_title: str
    url: str | None = None

    trigger: InputEvent
    screenshot_path: str = Field(description="Relative to the screens/ root")
    ocr_text: str = Field(default="", description="Redacted on write")
    ui_elements: list[UIElement] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Session (Phase 2 output)
# ---------------------------------------------------------------------------


class SessionCloseReason(str, Enum):
    DURATION_CAP = "duration_cap"
    IDLE_GAP = "idle_gap"
    CONTEXT_SHIFT = "context_shift"


class Session(BaseModel):
    """A bounded run of contiguous activity."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    session_id: str
    start_ts: datetime
    end_ts: datetime
    close_reason: SessionCloseReason
    event_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_ordered(self) -> "Session":
        if self.end_ts < self.start_ts:
            raise ValueError("end_ts must be >= start_ts")
        return self


# ---------------------------------------------------------------------------
# Label (Phase 4 output)
# ---------------------------------------------------------------------------


class CAGELabel(str, Enum):
    CAPTURE = "C"
    ANALYZE = "A"
    GENERATE = "G"
    EXTRACT = "E"


class ActionUnit(BaseModel):
    """Pass A output: a segmented action before classification."""

    model_config = ConfigDict(extra="forbid")

    action_id: str
    start_frame_id: str
    end_frame_id: str
    description: str = Field(description="One-line summary from Pass A")
    target_data_hint: str | None = None


class Label(BaseModel):
    """Pass B output: a classified action."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    action_id: str
    session_id: str
    cage_label: CAGELabel
    system: str
    data_object: str
    estimated_tokens: int = Field(
        ge=0,
        description=(
            "Claude's per-action estimate of how many tokens an agent would "
            "consume to perform this action end-to-end. Aggregate across many "
            "labeled actions to derive per-CAGE-class averages."
        ),
    )
    start_ts: datetime
    end_ts: datetime
    evidence_frame_ids: list[str] = Field(min_length=1)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    rationale: str

    @model_validator(mode="after")
    def _check_ordered(self) -> "Label":
        if self.end_ts < self.start_ts:
            raise ValueError("end_ts must be >= start_ts")
        return self
