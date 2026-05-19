"""Per-workflow labeler: incremental updates to a workflow graph (PoC).

Each Claude call ingests one session and updates the named workflow.
Repeated actions across sessions collapse into one node with an
observation_count. See SPEC.md when this gets revisited.
"""

from screen_workflow.labeler.api import (
    LabelerError,
    process_all_unprocessed_sessions,
    process_session,
)
from screen_workflow.labeler.batch import Batch, build_batch

__all__ = [
    "Batch",
    "build_batch",
    "process_session",
    "process_all_unprocessed_sessions",
    "LabelerError",
]
