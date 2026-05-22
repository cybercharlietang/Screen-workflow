"""Compose the per-session payload (event log + screenshots) for the labeler.

A Batch carries:
- the event log as a structured text table (always — cheap, covers every event)
- chronologically-ordered screenshots tagged with their frame_id

The system prompt is supplied by the labeler (``labeler/api.py``), not here.
Per-image token cost is approximated as ~1500 tokens (Claude's tiling cost for
a typical 1080p screenshot at default detail).
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

# Hard ceiling on screenshots sent to Claude per session. The event-log table
# always covers every event (text is cheap); only images are capped. A session
# with working dedupe lands well under this — the ceiling is a safety bound so
# a noisy session can't fan out into dozens of API calls. ~80 frames ≈ 14 chunks.
MAX_FRAMES_PER_SESSION = 80

# Anthropic per-request size cap is 32 MB. We send full-resolution PNGs
# (no compression — pixel detail matters for OCR + UI element reading) and
# split sessions into multiple chunks if the total payload would overflow.
# Conservative budget per-chunk leaves headroom for the prompt + JSON output.
# Smaller chunks are faster per-call, fail more cheaply if one errors,
# and give Claude more discrete mental-model-update steps. Tune up if
# rate-limited / latency-sensitive on larger sessions.
MAX_CHUNK_PAYLOAD_BYTES = 10 * 1024 * 1024   # ~10 MB of base64-encoded images
# Anthropic's "many-image request" 2000px-per-side rule appears to kick in
# above ~8-10 images per call. Staying under that lets us keep most
# screenshots at full PNG resolution. Tune up only if rate-limited.
MAX_IMAGES_PER_CHUNK = 6

# Anthropic also caps EACH image at 5 MB. We give ourselves headroom and
# only down-encode images that exceed this — smaller PNGs pass through
# at full quality.
MAX_PER_IMAGE_BYTES = 4 * 1024 * 1024
# And for multi-image requests, each image's largest dimension must be
# <= 2000 px. We resize anything that exceeds this regardless of file size.
MAX_IMAGE_DIMENSION_PX = 2000


@dataclass
class Batch:
    """Materialized request payload (one Anthropic call's worth of images)."""

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
    max_images: int,
) -> tuple[list[Event], list[Event]]:
    """Return (selected, dropped) capping the image set at ``max_images``.

    Keeps state-transition frames (saves, submits, focus/URL changes, pastes)
    plus the first and last frame, then fills the remainder by evenly-spaced
    sampling so the session stays visually represented end to end.
    """
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
    """Encode the screenshot to fit Anthropic's limits with minimum pixel loss.

    Strategy (in order — preserve resolution as long as possible):
      1. Raw PNG, if under both 5 MB and (only when batch is "many-image")
         the 2000 px dimension cap.
      2. JPEG at original dimensions, decreasing quality (95 → 80) until
         <= 5 MB. Preserves spatial resolution; only loses some quality.
      3. JPEG with progressive downscaling, last resort.

    The 2000 px cap only matters if the batch will be "many-image". We
    don't know the threshold exactly, but it's around ~10 images. The
    chunker keeps us under that, so we only enforce the dimension cap
    when we already need to compress for size anyway.
    """
    raw = path.read_bytes()
    with Image.open(path) as im:
        w, h = im.width, im.height

    if len(raw) <= MAX_PER_IMAGE_BYTES:
        return raw, "image/png"  # full PNG — preferred path

    # Step 2: JPEG at original size, decreasing quality.
    with Image.open(path) as im:
        im = im.convert("RGB")
        for quality in (95, 90, 85, 80):
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= MAX_PER_IMAGE_BYTES:
                log.info(
                    "compressed %s: %dx%d PNG %d KB -> JPEG q=%d (same size) %d KB",
                    path.name, w, h, len(raw) // 1024, quality, buf.tell() // 1024,
                )
                return buf.getvalue(), "image/jpeg"

        # Step 3: progressive downscale + JPEG. Hits the dim cap too.
        for max_px in (MAX_IMAGE_DIMENSION_PX - 100, 1600, 1280, 960, 720):
            for quality in (90, 85, 80, 70):
                im2 = im.copy()
                im2.thumbnail((max_px, max_px), Image.LANCZOS)
                buf = io.BytesIO()
                im2.save(buf, format="JPEG", quality=quality, optimize=True)
                if buf.tell() <= MAX_PER_IMAGE_BYTES and max(im2.size) <= MAX_IMAGE_DIMENSION_PX:
                    log.info(
                        "resized %s: %dx%d PNG %d KB -> JPEG q=%d %dx%d %d KB",
                        path.name, w, h, len(raw) // 1024,
                        quality, im2.width, im2.height, buf.tell() // 1024,
                    )
                    return buf.getvalue(), "image/jpeg"

    log.warning("could not shrink %s under caps; returning best effort", path)
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


def build_batch(events: list[Event], screens_root: Path) -> Batch:
    """Single-batch convenience wrapper — collapses all chunks into one batch.

    Useful for tests and small sessions. For real sessions prefer
    ``build_batches`` which splits into chunks under Anthropic's 32 MB cap.
    """
    chunks = build_batches(events, screens_root)
    if not chunks:
        return Batch(event_log_text=_event_log_table(events))
    if len(chunks) == 1:
        return chunks[0]
    # Merge — used only by tests; production calls build_batches and iterates.
    merged = Batch(
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


def build_batches(events: list[Event], screens_root: Path) -> list[Batch]:
    """Split a session into one or more Batches that each fit Anthropic's cap.

    Each returned Batch contains:
      - the FULL event log of the whole session (cheap text, always include)
      - a contiguous slice of images

    Images are capped at ``MAX_FRAMES_PER_SESSION`` per session (state-transition
    frames kept, the rest evenly sampled); the result is then chunked by
    accumulated base64 size and image-count cap.
    """
    if not events:
        return []

    event_log = _event_log_table(events)
    # Estimate of the non-image input: system prompt (~1.1K tokens, lives in
    # api.py) + the event-log table + the workflow directory + instructions.
    overhead = len(event_log) // 3 + 3_000

    # Cap images per session before the (expensive) PIL/base64 encode, so we
    # never encode frames we're going to drop.
    image_events, budget_dropped = _select_images(events, MAX_FRAMES_PER_SESSION)
    if budget_dropped:
        log.info(
            "session over %d-frame cap: sending %d image(s), dropping %d",
            MAX_FRAMES_PER_SESSION,
            len(image_events),
            len(budget_dropped),
        )

    # Build raw image blocks once (PIL/base64 happens here), then chunk
    image_records: list[tuple[Event, dict, dict, int]] = []
    for e in image_events:
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

    budget_dropped_ids = [ev.event_id for ev in budget_dropped]

    if not image_records:
        return [
            Batch(
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
                event_log_text=event_log,
                images=cur_images,
                selected_frame_ids=cur_ids,
                # Cap-dropped frames are recorded on the first batch only.
                dropped_frame_ids=budget_dropped_ids if not batches else [],
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
