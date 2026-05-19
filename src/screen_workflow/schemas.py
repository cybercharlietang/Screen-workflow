"""Cross-stage data contracts.

These pydantic models define the interface between every pipeline stage.
Changing them is a breaking change — bump ``SCHEMA_VERSION`` and update
``SPEC.md`` § 4 in the same commit.

See SPEC.md § 4 for how each model is produced and consumed.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 2


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
# Workflow graph (primary artifact)
# ---------------------------------------------------------------------------


class CAGELabel(str, Enum):
    CAPTURE = "C"
    ANALYZE = "A"
    GENERATE = "G"
    EXTRACT = "E"


class WorkflowNode(BaseModel):
    """One unique abstract action — repeated instances collapse to this node."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(description="Stable id, kebab-cased canonical_name")
    canonical_name: str = Field(description="Short human-friendly action name")
    cage_label: CAGELabel
    system: str = Field(description="App/system the action happens in")
    data_object_pattern: str = Field(
        description=(
            "Generic, not instance-specific. 'PO #<num>' rather than 'PO #12345'."
        )
    )
    estimated_tokens: int = Field(ge=0, description="Input+output of one LLM call")
    expected_agent_steps: int = Field(
        ge=1, default=1, description="LLM calls a real agent would make"
    )
    observation_count: int = Field(ge=0, default=0)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    rationale: str = ""


class WorkflowEdge(BaseModel):
    """Observed transition: after ``from_node`` we saw ``to_node`` happen."""

    model_config = ConfigDict(extra="forbid")

    from_node: str
    to_node: str
    observation_count: int = Field(ge=0, default=0)


class Workflow(BaseModel):
    """Master artifact: directed graph + goal-level summary.

    The graph is the evidence layer (what humans actually do, with costs).
    The goal/resources/trigger fields are the goal-oriented summary that an
    automating agent would consume — what to accomplish, what tools/data
    sources to use, what initiates the workflow.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    workflow_id: str
    name: str = Field(description="Human-friendly label, e.g. 'PO Approval'")

    # Goal-oriented summary (refined per call as evidence accumulates)
    goal: str = Field(
        default="",
        description="One sentence: what success looks like for this workflow.",
    )
    resources: list[str] = Field(
        default_factory=list,
        description="Systems / data sources / tools the workflow touches.",
    )
    trigger: str = Field(
        default="",
        description="What initiates this workflow (event, request, schedule).",
    )

    # Evidence graph
    nodes: dict[str, WorkflowNode] = Field(default_factory=dict)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    sessions_processed: list[str] = Field(default_factory=list)

    stable_observations: int = Field(
        ge=0,
        default=0,
        description=(
            "Consecutive update calls that added no new node or edge. Once "
            "this hits ``stability_threshold`` the workflow is marked complete."
        ),
    )
    stability_threshold: int = 20
    is_complete: bool = False
    noise_actions_count: int = Field(
        ge=0,
        default=0,
        description="Cumulative count of actions Claude marked as noise.",
    )
    created_at: datetime
    last_updated_at: datetime


class Observation(BaseModel):
    """Records that frames in a session mapped to a workflow node.

    Each observation carries its own per-instance token estimate; the
    node's headline ``estimated_tokens`` is the mean across all
    observations that map to it. Storing per-observation values lets us
    aggregate (mean / variance / outliers) instead of relying on one
    estimate made at node creation time.
    """

    model_config = ConfigDict(extra="forbid")

    observation_id: str
    workflow_id: str
    session_id: str
    node_id: str
    evidence_frame_ids: list[str] = Field(min_length=1)
    estimated_tokens: int = Field(
        ge=0,
        default=0,
        description="Per-instance: tokens an agent would use this time.",
    )
    expected_agent_steps: int = Field(ge=1, default=1)
    confidence: float = Field(ge=0.0, le=1.0)
    observed_at: datetime
