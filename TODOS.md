# TODOS.md

Phase-organized backlog. Move items between phases as priorities shift; do
not delete completed items (we want the historical trace) — strike them or
move under a "Done" section per phase.

---

## Phase 0 — Derisking (status: closed for code-side; annotation work continues in parallel)

- [x] Confirm signed ZDR (Zero-Data-Retention) agreement with Anthropic.
- [x] Legal sign-off in place.
- [~] 2 internal annotators are designing ~5 procurement workflows with
      noise + ideal CAGE-labeled traces (golden-output dataset). Inter-rater
      κ to be measured by independent cross-labeling per
      `docs/annotation_prompt.md`. Tracked separately from code work.
- [x] Local encryption mechanism decided: **SQLCipher** for `events.db` +
      **Fernet** for `screens/*.png`, master key wrapped via Windows DPAPI.
      Defense-in-depth above BitLocker.
- [ ] Redaction list — still needs legal + client sign-off on signal vs.
      PII (vendor name, PO #, GL code, $ are signal; SSN, full card #,
      password fields are masked). Daemon will treat as config, so list
      can be finalized in parallel with Phase 1.

## Cross-cutting — Visualizer and tests (built alongside every phase)

A **static-HTML report generator** at `src/screen_workflow/viz/`. After any
phase produces output, run `python -m screen_workflow.viz <session_id>` to
emit `viz_output/<session>/index.html` — a single self-contained file with
images base64-inlined and JSON data inlined in `<script>` tags. No server,
no CORS, no Python required to view. Double-click the file, browser opens.

The HTML has tabbed sections that fill in as phases ship. Each phase also
produces a `tests/fixtures/phaseN/` canned dataset that doubles as the
report's demo data, so we never need to capture live screens to verify.

- [ ] `viz/` package + `__main__.py` CLI entry; reads the local DB +
      `screens/` and emits a static HTML file.
- [ ] Base HTML template + vanilla-JS tab switcher (no framework, no
      build step).
- [ ] **Section — Events** (Phase 1): chronological event table; row
      click → screenshot + OCR text + UI tree dump.
- [ ] **Section — Sessions** (Phase 2): timeline with session boundaries.
- [ ] **Section — Batches** (Phase 3): preview of the Claude request
      (event-log table + selected images + token count, no API call).
- [ ] **Section — Labels** (Phase 4): input frames + Claude's segmentation +
      CAGE labels + confidence + rationale, side by side.
- [ ] **Section — Cost** (Phase 5): aggregate token-cost estimates.
- [ ] Per-phase test layout: `tests/test_phase{N}_*.py` + fixture dir.
      Use snapshot tests (`syrupy` or hand-rolled) for stage outputs.
- [ ] `--smoke` mode on the daemon: 60 s real capture into a sandbox DB
      for local manual verification (not run in CI).

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
