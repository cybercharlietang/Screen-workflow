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

# Anthropic per-request size cap is 32 MB. We send full-resolution PNGs
# (no compression — pixel detail matters for OCR + UI element reading) and
# split sessions into multiple chunks if the total payload would overflow.
# Conservative budget per-chunk leaves headroom for the prompt + JSON output.
MAX_CHUNK_PAYLOAD_BYTES = 20 * 1024 * 1024   # ~20 MB of base64-encoded images
MAX_IMAGES_PER_CHUNK = 25                     # safety cap regardless of size

# Anthropic also caps EACH image at 5 MB. We give ourselves headroom and
# only down-encode images that exceed this — smaller PNGs pass through
# at full quality.
MAX_PER_IMAGE_BYTES = 4 * 1024 * 1024
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


def _shrink_for_anthropic(path: Path) -> tuple[bytes, str]:
    """If the screenshot exceeds the per-image cap, shrink it; otherwise
    return the raw PNG bytes. Tries progressively smaller resolutions
    and JPEG qualities until the result fits."""
    raw = path.read_bytes()
    if len(raw) <= MAX_PER_IMAGE_BYTES:
        return raw, "image/png"

    with Image.open(path) as im:
        im = im.convert("RGB")
        original_size = (im.width, im.height)
        for max_px in (2048, 1600, 1280, 960, 720):
            for quality in (90, 80, 70):
                im2 = im.copy()
                im2.thumbnail((max_px, max_px), Image.LANCZOS)
                buf = io.BytesIO()
                im2.save(buf, format="JPEG", quality=quality, optimize=True)
                if buf.tell() <= MAX_PER_IMAGE_BYTES:
                    log.info(
                        "compressed %s: %dx%d PNG %d KB -> JPEG q=%d %dpx %d KB",
                        path.name,
                        *original_size,
                        len(raw) // 1024,
                        quality,
                        max_px,
                        buf.tell() // 1024,
                    )
                    return buf.getvalue(), "image/jpeg"
    # Last resort — return whatever the smallest attempt produced, even
    # if it's still over (Anthropic will reject; let caller surface).
    log.warning("could not shrink %s under cap; returning best effort", path)
    return buf.getvalue(), "image/jpeg"


def _image_block(event: Event, screens_root: Path) -> dict | None:
    """Encode the screenshot as a base64 image content block.

    Full-quality PNG by default; auto-compresses if the original would
    exceed Anthropic's per-image 5 MB cap.
    """
    rel = event.screenshot_path.replace("\\", "/")  # Win/POSIX portability
    p = screens_root / rel
    if not p.exists():
        log.warning("screenshot missing: %s", p)
        return None
    try:
        img_bytes, media_type = _shrink_for_anthropic(p)
    except Exception:  # noqa: BLE001
        log.exception("failed to encode %s", p)
        return None
    data = base64.b64encode(img_bytes).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        },
    }


def build_batch(
    events: list[Event],
    screens_root: Path,
    budget_tokens: int = DEFAULT_BUDGET,
) -> Batch:
    """Single-batch convenience wrapper — collapses all chunks into one batch.

    Useful for tests and small sessions. For real sessions prefer
    ``build_batches`` which splits into chunks under Anthropic's 32 MB cap.
    """
    chunks = build_batches(events, screens_root, budget_tokens)
    if not chunks:
        return Batch(system=SYSTEM_PROMPT, event_log_text=_event_log_table(events))
    if len(chunks) == 1:
        return chunks[0]
    # Merge — used only by tests; production calls build_batches and iterates.
    merged = Batch(
        system=SYSTEM_PROMPT,
        event_log_text=chunks[0].event_log_text,
        images=[],
        selected_frame_ids=[],
        dropped_frame_ids=[],
        approx_input_tokens=0,
    )
    for c in chunks:
        merged.images.extend(c.images)
        merged.selected_frame_ids.extend(c.selected_frame_ids)
        merged.dropped_frame_ids.extend(c.dropped_frame_ids)
        merged.approx_input_tokens += c.approx_input_tokens
    return merged


def build_batches(
    events: list[Event],
    screens_root: Path,
    budget_tokens: int = DEFAULT_BUDGET,
) -> list[Batch]:
    """Split a session into one or more Batches that each fit Anthropic's cap.

    Each returned Batch contains:
      - the FULL event log of the whole session (cheap text, always include)
      - a contiguous slice of images
    Chunking is by accumulated base64 size and by image count cap.
    """
    if not events:
        return []

    event_log = _event_log_table(events)
    overhead = len(SYSTEM_PROMPT) // 3 + len(event_log) // 3 + 2_000

    # Build raw image blocks once (PIL/base64 happens here), then chunk
    image_records: list[tuple[Event, dict, dict, int]] = []
    for e in events:
        block = _image_block(e, screens_root)
        if block is None:
            continue
        marker = {
            "type": "text",
            "text": f"frame_id={e.event_id} ts={e.ts.isoformat(timespec='seconds')}",
        }
        # base64 payload byte size approximates the on-the-wire cost
        size_bytes = len(block["source"]["data"])
        image_records.append((e, marker, block, size_bytes))

    if not image_records:
        return [
            Batch(
                system=SYSTEM_PROMPT,
                event_log_text=event_log,
                images=[],
                selected_frame_ids=[],
                dropped_frame_ids=[ev.event_id for ev in events],
                approx_input_tokens=overhead,
            )
        ]

    batches: list[Batch] = []
    cur_images: list[dict] = []
    cur_ids: list[str] = []
    cur_bytes = 0

    def _flush() -> None:
        nonlocal cur_images, cur_ids, cur_bytes
        if not cur_ids:
            return
        batches.append(
            Batch(
                system=SYSTEM_PROMPT,
                event_log_text=event_log,
                images=cur_images,
                selected_frame_ids=cur_ids,
                dropped_frame_ids=[],
                approx_input_tokens=overhead + len(cur_ids) * APPROX_TOKENS_PER_IMAGE,
            )
        )
        cur_images = []
        cur_ids = []
        cur_bytes = 0

    for ev, marker, block, size_bytes in image_records:
        # If adding this image would blow the chunk, flush first.
        would_overflow = (
            cur_bytes + size_bytes > MAX_CHUNK_PAYLOAD_BYTES
            or len(cur_ids) >= MAX_IMAGES_PER_CHUNK
        )
        if would_overflow and cur_ids:
            _flush()
        cur_images.append(marker)
        cur_images.append(block)
        cur_ids.append(ev.event_id)
        cur_bytes += size_bytes

    _flush()

    log.info(
        "split %d images into %d batch(es); sizes: %s",
        len(image_records),
        len(batches),
        [f"{len(b.selected_frame_ids)} images" for b in batches],
    )
    return batches
