"""Compose a multimodal Claude request from one session.

Includes:
- system prompt with CAGE taxonomy and output schema
- the event log as a structured text table (always — cheap)
- chronologically-ordered screenshots tagged with their frame_id

Token budget is enforced by downsampling screenshots if needed. Per-image
token cost is approximated as ~1500 tokens (Claude's tiling cost for a
typical 1080p screenshot at default detail).
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from screen_workflow.schemas import Event

log = logging.getLogger(__name__)

APPROX_TOKENS_PER_IMAGE = 1500
DEFAULT_BUDGET = 400_000  # leave headroom below 500K for response generation

# Anthropic per-request size cap is 32 MB. Full-resolution 1080p PNGs are
# ~1.5 MB each, so a session of 30+ frames overflows that. We resize the
# longest edge to 1280px and JPEG-encode at q=80 — still highly readable
# for the model (text and UI elements remain crisp) but ~50-150 KB per
# image, so a 100-image batch is comfortably ~10 MB.
IMAGE_RESIZE_MAX_PX = 1280
IMAGE_JPEG_QUALITY = 80
SYSTEM_PROMPT = """\
You are a procurement-workflow analyst at Fragment. You will be shown a
chronological session of one employee's screen activity: a structured event
log table and the corresponding screenshots.

Your job: identify the discrete *cognitive actions* the user performed and
label each one under the **CAGE** taxonomy:

  - C — Capture: ingesting data into the user's working memory.
        Reading an email, opening a vendor record, downloading a PDF.
  - A — Analyze: interpreting, comparing, reasoning over captured data.
        Reconciling invoice lines, checking budget, deciding vendors.
  - G — Generate: producing new content.
        Drafting an approval email, writing free-text comments.
  - E — Extract: pulling structured fields from unstructured sources.
        OCR'ing an invoice, copying numbers into a form.

CRUCIAL guidelines:

1. **Merge into cognitive units.** Five clicks while filling one form is
   ONE action ("filled out vendor form"), not five. Match the natural
   one-line description a human reviewer would give.
2. **Ignore noise.** Slack pings, lunch breaks, unrelated browsing —
   omit. Only return procurement-relevant actions.
3. **Be specific in `data_object`.** "PO #12345" beats "purchase order."
   If you can't tell, write "(unknown)".
4. **`estimated_tokens` is the input+output cost of ONE LLM call** an
   agent would make to perform this action — context it reads + its
   reasoning + its output. Typical ranges:
     - Capture / Extract (reading short content): 500–3,000
     - Analyze (comparison/reasoning): 2,000–8,000
     - Generate (drafting an email, report): 1,500–10,000
   Use the upper end when the action involves long documents.
5. **`expected_agent_steps` is the count of distinct LLM calls** the
   agent would need:
     - 1 for a single read+act (e.g., "open this email")
     - 2–3 for compare/decide (e.g., "reconcile invoice vs PO")
     - 4+ only for genuinely multi-step actions
6. **`confidence` 0.0–1.0** — be honest. If the screenshots don't
   support the label well, drop below 0.6.
7. **Cite evidence.** `evidence_frame_ids` must reference real
   `frame_id` values from the input.

Return ONLY a JSON object of the form:

{
  "actions": [
    {
      "action_id": "act_1",
      "cage_label": "C|A|G|E",
      "system": "Outlook|SAP|Excel|Chrome|...",
      "data_object": "...",
      "estimated_tokens": 2500,
      "expected_agent_steps": 1,
      "start_frame_id": "<id>",
      "end_frame_id": "<id>",
      "evidence_frame_ids": ["<id>", ...],
      "confidence": 0.82,
      "rationale": "one short sentence"
    }
  ]
}

No prose outside the JSON.
"""


@dataclass
class Batch:
    """Materialized request payload."""

    system: str
    event_log_text: str
    images: list[dict] = field(default_factory=list)  # anthropic content blocks
    selected_frame_ids: list[str] = field(default_factory=list)
    dropped_frame_ids: list[str] = field(default_factory=list)
    approx_input_tokens: int = 0


def _event_log_table(events: list[Event]) -> str:
    rows = ["frame_id\tts\tapp\twindow_title\ttrigger\ttarget"]
    for e in events:
        rows.append(
            "\t".join(
                [
                    e.event_id,
                    e.ts.isoformat(timespec="seconds"),
                    e.app,
                    (e.window_title or "")[:80].replace("\t", " "),
                    e.trigger.type.value,
                    (e.trigger.target_label or "").replace("\t", " "),
                ]
            )
        )
    return "\n".join(rows)


def _select_images(
    events: list[Event],
    screens_root: Path,
    budget_tokens: int,
    overhead_tokens: int,
) -> tuple[list[Event], list[Event]]:
    """Return (selected, dropped) such that selected.images fit budget."""
    available = budget_tokens - overhead_tokens
    max_images = max(0, available // APPROX_TOKENS_PER_IMAGE)

    if len(events) <= max_images:
        return events, []

    # Always-keep set: trigger-driven non-heartbeat events.
    must_keep_idx: set[int] = set()
    for i, e in enumerate(events):
        if e.trigger.type.value in (
            "save",
            "submit",
            "url_change",
            "file_open",
            "file_save",
            "window_focus",
            "paste",
        ):
            must_keep_idx.add(i)
    # And the first / last frame.
    must_keep_idx.add(0)
    must_keep_idx.add(len(events) - 1)

    # If must_keep alone exceeds budget, trim from the middle.
    must_keep_idx_list = sorted(must_keep_idx)
    if len(must_keep_idx_list) > max_images:
        # keep first, last, and evenly-spaced subset
        keep = {must_keep_idx_list[0], must_keep_idx_list[-1]}
        stride = max(1, len(must_keep_idx_list) // max_images)
        for i in range(0, len(must_keep_idx_list), stride):
            keep.add(must_keep_idx_list[i])
            if len(keep) >= max_images:
                break
        selected_idx = sorted(keep)[:max_images]
    else:
        # fill the remainder by evenly-spaced sampling of the rest
        remaining = [i for i in range(len(events)) if i not in must_keep_idx]
        room = max_images - len(must_keep_idx_list)
        if remaining and room > 0:
            stride = max(1, len(remaining) // room)
            extras = remaining[::stride][:room]
            selected_idx = sorted(must_keep_idx_list + list(extras))
        else:
            selected_idx = must_keep_idx_list

    chosen = set(selected_idx)
    selected = [events[i] for i in range(len(events)) if i in chosen]
    dropped = [events[i] for i in range(len(events)) if i not in chosen]
    return selected, dropped


def _image_block(event: Event, screens_root: Path) -> dict | None:
    """Resize + JPEG-encode the screenshot for the Anthropic request.

    Full-res PNGs are too large for the 32 MB per-request cap once you have
    20+ frames. 1280px JPEG q=80 keeps text and UI elements readable while
    shrinking each frame to ~50-150 KB.
    """
    # Normalize backslashes to forward slashes so paths stored on Windows
    # still resolve on POSIX (and remain valid on Windows).
    rel = event.screenshot_path.replace("\\", "/")
    p = screens_root / rel
    if not p.exists():
        log.warning("screenshot missing: %s", p)
        return None
    try:
        with Image.open(p) as im:
            im = im.convert("RGB")
            im.thumbnail((IMAGE_RESIZE_MAX_PX, IMAGE_RESIZE_MAX_PX), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
            jpeg_bytes = buf.getvalue()
    except Exception:  # noqa: BLE001
        log.exception("failed to resize %s; skipping image", p)
        return None
    data = base64.b64encode(jpeg_bytes).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": data,
        },
    }


def build_batch(
    events: list[Event],
    screens_root: Path,
    budget_tokens: int = DEFAULT_BUDGET,
) -> Batch:
    """Compose a Batch ready to be sent as ``messages`` content to Claude."""
    event_log = _event_log_table(events)
    overhead = len(SYSTEM_PROMPT) // 3 + len(event_log) // 3 + 2_000

    selected, dropped = _select_images(events, screens_root, budget_tokens, overhead)

    images: list[dict] = []
    selected_ids: list[str] = []
    for e in selected:
        # Annotate frame with a small text marker before the image so
        # Claude can reliably cite frame_ids in its output.
        images.append({"type": "text", "text": f"frame_id={e.event_id} ts={e.ts.isoformat(timespec='seconds')}"})
        block = _image_block(e, screens_root)
        if block is None:
            continue
        images.append(block)
        selected_ids.append(e.event_id)

    approx = overhead + len(selected_ids) * APPROX_TOKENS_PER_IMAGE
    return Batch(
        system=SYSTEM_PROMPT,
        event_log_text=event_log,
        images=images,
        selected_frame_ids=selected_ids,
        dropped_frame_ids=[d.event_id for d in dropped],
        approx_input_tokens=approx,
    )
