# TODOS.md

Phase-organized backlog. Move items between phases as priorities shift; do
not delete completed items (we want the historical trace) — strike them or
move under a "Done" section per phase.

---

## Phase 0 — Derisking (do these before serious code)

- [ ] Confirm signed ZDR (Zero-Data-Retention) agreement with Anthropic
      covering the API key used by the labeler.
- [ ] Draft initial redaction list with legal + client: which procurement
      fields are signal we must keep (vendor name, PO #, GL code, $) vs.
      PII we must mask. See `LESSONS.md` § L7.
- [ ] Recruit 2 internal annotators; label 10–20 hours of internal screen
      activity manually under the CAGE taxonomy. Measure inter-rater
      agreement; refine the taxonomy until agreement is acceptable
      (target Cohen's κ ≥ 0.7). See `LESSONS.md` § L6.
- [ ] Decide local DB encryption-at-rest mechanism (SQLCipher vs.
      `cryptography` Fernet over the file).

## Phase 1 — Capture daemon (Windows)

Sub-package: `src/screen_workflow/capture/` + `enrich/` + `storage/`.

- [ ] Native event hooks: mouse clicks (`pynput`), keyboard chords,
      window focus + title (`pywinctl`), active process (`psutil`).
- [ ] Trigger logic: classify raw events as "meaningful" vs. ignore.
- [ ] Screenshot module on trigger + 30 s heartbeat. Pillow grab of the
      foreground monitor only (not all monitors by default — privacy).
- [ ] Perceptual-hash frame dedupe.
- [ ] Local enricher:
  - [ ] OCR via Windows.Media.Ocr (`winrt` bindings).
  - [ ] UI element tree via UI Automation (`pywinauto` or `comtypes`).
  - [ ] Foreground browser URL via a Chrome/Edge extension that writes to
        a local IPC socket the daemon polls.
- [ ] Redactor: regex + heuristics, configurable redaction-list file.
- [ ] Local sink: encrypted SQLite (`events.db`) + screenshots dir.
      Rolling retention (default 7 d).
- [ ] Pydantic `Event` model defining the cross-stage contract.
- [ ] Daemon main loop, structured logging, graceful shutdown.

## Phase 2 — Session segmenter

Sub-package: `src/screen_workflow/session/`.

- [ ] Heuristic segmenter: 30 min hard cap, idle gap > 2 min,
      context-shift > 60 s of non-procurement foreground.
- [ ] Pydantic `Session` model.
- [ ] CLI: list / inspect / re-open sessions for debugging.

## Phase 3 — Batch builder

Sub-package: `src/screen_workflow/labeler/batch.py`.

- [ ] Token counter (use Anthropic SDK token-counting endpoint).
- [ ] Image selection: keep boundaries, keep trigger frames, fill the
      rest by visual diversity (drop pHash-near-duplicates of already
      selected frames). Enforce 500 K budget.
- [ ] Compose multimodal request (system prompt + event-log table +
      ordered images each tagged with `frame_id` + `ts`).
- [ ] Dry-run mode: print request shape and token count without calling
      the API.

## Phase 4 — Claude labeler

Sub-package: `src/screen_workflow/labeler/`.

- [ ] Pass A — segmentation prompt; returns list of action units.
- [ ] Pass B — CAGE classification prompt; returns labeled actions.
- [ ] Output validation against pydantic `Label` model.
- [ ] Retry / backoff; cache Pass A output keyed on session hash.
- [ ] Per-call telemetry: tokens in/out, latency, cost.

## Phase 5 — Aggregation + HITL

Sub-package: `src/screen_workflow/analytics/`.

- [ ] Export labels to analytics DB (start with Postgres; revisit BQ).
- [ ] Review queue: sample 5% of sessions + all low-confidence; web UI
      for re-labeling.
- [ ] Cost model: `tokens_per_complexity[label][complexity]` table,
      seeded from real agent runs we benchmark ourselves.
- [ ] Workflow report: per-employee, per-team, per-process token-cost
      estimate.

## Phase 6 — Pilot + iteration

- [ ] One-employee pilot for one week. Iterate taxonomy and prompts on
      real data.
- [ ] Per-employee opt-in UX + visible on/off control on the daemon.

## Open questions

- ZDR contract signed, with what scope?
- Final redaction list contents?
- Analytics DB: Postgres vs. BigQuery?
- Browser extension scope: Chrome only, or also Edge/Firefox?
- How do we handle multi-monitor users? (default: foreground monitor only)
- How do we handle non-English UIs / OCR? (defer to v2)

## Done

- [x] Initial design conversation and pipeline shape — captured in
      `SPEC.md` and `LESSONS.md`.
- [x] Repo scaffold.
