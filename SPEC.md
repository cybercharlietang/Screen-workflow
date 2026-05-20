# Screen-workflow — Specification

Status: **v1.0 — PoC complete** (2026-05-19). This file reflects the
architecture as shipped, not the original design (which differed in
several material ways — noted inline where the implementation diverged).
Update when architecture changes; do not let code and spec drift.

## Implementation notes (vs. original design)

The shipped system differs from the v0.1 spec on three fronts:

1. **The artifact.** Original plan: per-session lists of CAGE-labeled
   "actions" with start/end timestamps + complexity tier. Shipped:
   a **workflow graph** per workflow (unique nodes with observation
   counts, edges with transition counts) plus a goal-oriented summary
   (goal / resources / trigger). Per-session label dumps are no longer
   stored; instead `Observation` rows record which frames mapped to
   which node. See `LESSONS.md` § L12.

2. **Routing.** Original plan: one session = one workflow (manual
   `--workflow` flag). Shipped: **per-action routing** — each cognitive
   action is independently routed to an existing workflow, a new
   workflow (Claude names it), or noise. A session can contribute to
   multiple workflows simultaneously. See `LESSONS.md` § L13.

3. **Labeling structure.** Original plan: two-pass (segment then
   classify). Shipped: single-pass per chunk, multiple chunks per
   session if needed (each chunk ≤ 6 images / ≤ 10 MB to stay under
   Anthropic's per-image and per-request caps). The workflow graph
   itself acts as the cross-call mental model — each chunk's Claude
   call sees the directory of workflows built up by previous chunks.

Cost data from the demo run (Opus 4.7): ~$0.80 per 10-min employee
session. ~$30/employee/day extrapolation at scale.

---

## 1. Problem

The company automates procurement processes for clients. To estimate the
agent-token cost of automating a client's workflow we first need to know what
the workflow actually is. Clients' workflows are tacit knowledge that lives in
employees' day-to-day screen activity (ERP screens, spreadsheets, browser
research, email approvals, etc.). We need to:

1. Capture screen activity unobtrusively on employee machines.
2. Filter out non-workflow noise (lunch breaks, unrelated browsing).
3. Segment the remaining activity into discrete **actions**.
4. Classify each action under the **CAGE** taxonomy.
5. Use the labeled action stream to estimate the per-workflow token cost of
   an automated agent that performs the same work.

## 2. CAGE taxonomy

Every workflow action is exactly one of:

- **C — Capture**: ingesting data into the worker's local context. Reading a
  PO email, opening a vendor record in the ERP, downloading an invoice PDF.
- **A — Analyze**: interpreting / comparing / reasoning over captured data.
  Reconciling line items, checking a budget, deciding which vendor to use.
- **G — Generate**: producing new content. Drafting an approval email,
  writing free-text comments, generating a report.
- **E — Extract**: pulling structured fields out of unstructured or
  semi-structured sources. OCR'ing an invoice, copying numbers into a form.

The taxonomy will be validated and refined against human-labeled sessions
before being treated as load-bearing. See `TODOS.md` § Phase 0.

## 3. Privacy and data model

**Hard boundary**: raw screenshots and raw OCR text never leave the employee
machine in persistent form. They may transit to the Claude API for labeling
under a Zero-Data-Retention (ZDR) agreement so Anthropic does not retain
them. Only the **labeled, redacted, structured outputs** returned by Claude
are persisted in the company's analytics store.

```
Employee machine                    │ Anthropic (ZDR)        │ Company cloud
───────────────────────────────────────────────────────────────────────────
local SQLite + screenshots/         │                        │
(encrypted-at-rest, TTL'd)          │                        │
                                    │                        │
batch builder ─── multimodal req ──▶│  Claude labeler        │
                                    │  (no retention)        │
labels (text JSON)                  │ ◀───── response ───────│
        │                                                    │
        └─── push labels only ───────────────────────────────▶ analytics DB
```

Open legal questions (tracked in `TODOS.md`):

- Confirm ZDR agreement scope and signed contract terms.
- Define the redaction list: which procurement fields are signal (vendor
  name, PO #, dollar amount, GL code) vs. PII to mask.
- Retention TTL for local screenshots (default 7 days, configurable).

## 4. Pipeline

Five components, each with a typed input/output contract so they can be
developed and tested independently.

### 4.1 Capture daemon (local)

Single long-running Python process on the employee machine.

- **Event listener** subscribes to native OS events using `pynput` (mouse,
  keyboard) and `pywinctl`/UI Automation (window focus, title changes). A
  browser extension provides URL + page title for higher-fidelity browser
  context.
- **Trigger logic** classifies each raw event as "meaningful" (warrants a
  screenshot) or not:
  - Yes: window focus change, click on an interactive UI element, paste,
    form submit, file open/save, URL change, save-key combos.
  - No: raw mousemove, modifier-only keys, held keys.
- **Heartbeat**: one screenshot every 30 s regardless, as a safety net for
  long Analyze actions with no input events.
- **Frame dedupe**: perceptual hash (pHash) — drop the new frame if it
  matches the previous kept frame.
- **Local enricher** per kept frame:
  - OCR via Windows.Media.Ocr (v1; Tesseract fallback).
  - UI element tree via UI Automation: role, label, bbox per element.
  - Active process name (`psutil`), window title, foreground URL (browser
    extension), focused control.
- **Redactor**: regex + heuristic masking applied *before* writing to disk.
  Masks password fields (by accessibility role), full SSN/credit card
  patterns, configurable PII patterns. The redaction list is the privacy
  firewall — review with legal before adding fields.
- **Local sink**: append to encrypted SQLite (`events.db`) + screenshots
  directory (`./screens/<yyyy>/<mm>/<dd>/`). Rolling retention TTL.

### 4.2 Session segmenter (local)

Runs every few minutes over recent events. Emits a closed session when any
of:

- Wall-clock 30 min elapsed since session start.
- Idle gap > 2 min (no input events).
- Foreground app fully shifted to a non-procurement context > 60 s.

Output is a session manifest pointing at row IDs in `events.db`.

### 4.3 Batch builder (local)

Turns one closed session into one Claude request.

- Token budget target: **500 K tokens** (the 1 M context degrades past ~600 K).
- Composition:
  - System prompt + CAGE taxonomy: ~5 K
  - Event log as a structured text table (always include all kept frames as
    rows): ~50–150 K
  - Screenshots, chronological, each labeled with `ts` and `frame_id` so
    Claude can cite frame IDs in output: filled to remaining budget at
    ~1.5 K tokens per image (so ~200–250 image cap).
- Image selection when too many frames:
  - Always include first + last frame per app-context.
  - Always include frames whose trigger is `save`, `submit`, `url_change`.
  - Fill the rest by maximum visual diversity (drop near-duplicates of
    already-selected frames).

### 4.4 Claude labeler (cloud, two passes)

**Pass A — segmentation.** Input: event-log text + a downsampled set of
images. Output: a list of *action units* with `start_frame_id`,
`end_frame_id`, one-line description, suspected target data object. Cheaper
because we do not need every image to find action boundaries.

**Pass B — CAGE classification.** Input: each action unit with all its
frames. Output per action:

```json
{
  "action_id": "...",
  "cage_label": "C|A|G|E",
  "system": "SAP|Outlook|Chrome|Excel|...",
  "data_object": "PO #12345 | Vendor record | Budget line | ...",
  "complexity": "S|M|L",
  "start_ts": "...",
  "end_ts": "...",
  "evidence_frame_ids": ["..."],
  "confidence": 0.0-1.0,
  "rationale": "..."
}
```

Splitting into two passes (a) keeps each call inside the sharp part of the
context window, (b) lets us cache segmentation if classification prompts
change, (c) lets us re-run classification when the taxonomy evolves without
re-segmenting.

### 4.5 Aggregation + HITL store (company cloud)

- Labels land in Postgres (or BigQuery — TBD).
- A **review queue** samples 5 % of sessions plus all low-confidence ones
  for human relabeling. Human labels are the ground truth and the input to
  taxonomy iteration.
- **Cost model**: `cost_per_workflow = Σ tokens_per_complexity[cage_label][complexity]`.
  The `tokens_per_complexity` table is calibrated from real agent runs we
  perform ourselves, not guessed.

## 5. Tech stack

- **Language**: Python 3.11+.
- **OS (v1)**: Windows 10/11.
- **Key libraries**: `pynput`, `pywinctl`, `psutil`, `Pillow`, `imagehash`,
  `anthropic`, `pydantic`, `sqlite3` (stdlib), `cryptography` (for
  encryption-at-rest of the local DB and screenshot dir).
- **OCR**: Windows.Media.Ocr via `winrt` (preferred); Tesseract via
  `pytesseract` (fallback).
- **UI tree**: Windows UI Automation via `comtypes` / `pywinauto`.
- **Packaging**: `pyproject.toml`, single console script `screen-workflowd`.

## 6. Open questions

Tracked in `TODOS.md` § Open questions. Highlights:

- ZDR contract signed?
- Final redaction list (signal vs. PII)?
- Storage choice for analytics DB (Postgres vs. BigQuery)?
- Browser extension scope (Chrome only, or also Edge/Firefox)?
- Per-employee opt-in UX + on/off control on the daemon?
