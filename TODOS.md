# TODOS.md

Phase-organized backlog. Move items between phases as priorities shift; do
not delete completed items (we want the historical trace) — strike them or
move under a "Done" section per phase.

## Status as of 2026-05-19: PoC COMPLETE ✓

End-to-end pipeline working: capture → segment → label → workflow graph
→ viz. Real demo session produced "Monitor purchase recommendation"
workflow (6 nodes, ~$0.80 in API cost for 10 min of activity). 58 tests
passing. See `docs/prompts.md` for the active Claude prompts and
`LESSONS.md` § L9–L16 for what we learned along the way.

### Next 3 steps for whoever picks this up

1. **Annotator validation pass.** Run the hand-labeled gold-output
   workflows (your colleague's parallel work, see `docs/annotation_prompt.md`)
   through the labeler. Compute per-class agreement (Cohen's κ ≥ 0.7
   target). Use disagreements to tune the system prompt in
   `labeler/api.py:SYSTEM_PROMPT`.
2. **HITL review queue.** Build a simple "Review" tab in the viz that
   lists workflows nearing or at the stability threshold
   (`is_complete=True`). Approve/reject button writes back to the DB.
   The flagging mechanism is already in place — needs a UI.
3. **Token-estimate calibration.** Write `bench/agent_cost_calibration.py`
   that runs a real agent against 3 canonical actions per CAGE class
   (Capture/Analyze/Generate/Extract) and produces a
   `tokens_per_action[cage_label]` mean/variance table. Replace Claude's
   per-action estimates with this calibrated lookup for defensible cost
   numbers.

## PoC scope (today, ~5–6 h budget)

Hard cuts to fit a one-day proof of concept:

- **No SQLCipher / DPAPI.** Plain SQLite + plain `screens/` dir on a
  BitLocker-protected machine. Encryption-at-rest deferred.
- **No UI Automation tree.** Window title + OCR carries enough signal for
  the PoC. UI tree deferred.
- **No browser extension.** Window title only for browser context.
- **Minimal session segmenter:** 30-min hard cap + 2-min idle gap. Context-
  shift detection deferred.
- **Minimal redaction:** mask only obvious patterns (SSN, full card #).
  Full config-driven redaction list deferred to legal sign-off.
- **No HITL review queue UI** (Phase 5). Manual inspection via the
  static-HTML viewer is enough.
- **Cross-platform where free.** `mss` + `pynput` + Tesseract work on
  Linux/Mac too, useful for dev. Production target is still Windows.

Critical path for today: capture → store → batch → label → view → cost.
Everything else is iteration.

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

## Production hardening (deferred from PoC)

These were intentionally cut for PoC scope. Revisit when going to pilot:

- [ ] SQLCipher + DPAPI encryption of `events.db` and `screens/*.png`.
- [ ] Local OCR + UI Automation tree per frame (vs. relying on Claude
      to OCR each screenshot every call).
- [ ] Browser extension for URL fidelity (currently window title only).
- [ ] Prompt caching with Anthropic's prompt-cache feature on the
      workflow directory (saves money once the directory grows).
- [ ] Configurable image quality: a `--compress` flag on the labeler to
      pre-shrink screenshots before sending (trade fidelity for cost at
      scale).
- [ ] Cross-platform daemon (macOS + Linux), pyobjc Accessibility +
      AT-SPI bindings.
- [ ] Redaction list signed off with legal (deferred since PoC ran on
      internal data only).
- [ ] Auto-routing improvements: today the labeler relies on Claude
      naming workflows; with many workflows in the directory the
      summaries should be cached or chunked.

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

### Prototype-hardening pass (2026-06-09)

- [x] Continuous live pipeline: capture + segment + label + viz in one
      process, with cost monitor (soft/hard/cap guards) + loud no-key alert.
- [x] Rolling flush: sessions flush to the labeler every 40 events / 5 min
      regardless of idle (decoupled from session semantics).
- [x] Dedup: recent-hash ring buffer (kills A→B→A), window-focus title-flap
      gate, switchable `--hash-mode perceptual|exact` + stats instrumentation.
- [x] Triggers: click capture-after-settle, Enter=submit typing-precision,
      **new-window / dialog-open trigger** (decision points, always kept).
- [x] `--max-image-px` knob (default 1568; below that cuts image tokens) —
      replaces the deferred `--compress` item.
- [x] Per-run metrics: `runs/run_<stamp>.json` + `runs.jsonl` for cross-run
      comparison (`analytics/metrics.py`).
- [x] Key handling: gitignored `.env` auto-load + double-click `start.bat`.

### Next (post-baseline)

- [ ] Re-run with the new dedup + measure frame-count/cost delta vs L17 baseline.
- [ ] Trim output JSON verbosity (~29% of cost) and per-call overhead (~40%).
- [ ] Clipboard-change trigger (data-source lineage); content capture is
      redaction-gated.
- [ ] Novelty/semantic dedup (suppress model-redundant frames vs stable
      nodes) — deferred until baseline shows the redundancy is there to harvest.
