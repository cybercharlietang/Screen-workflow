# LESSONS.md

Non-trivial things we have learned. **Append; do not rewrite history.** Each
entry: short rule first, then *Why* (the reasoning or the incident), then
*How to apply* (when this kicks in).

---

## L1 — "Encrypt before sending to Claude" is not a privacy mechanism

**Rule.** Transit encryption (TLS) does not change what Claude sees. The
model must decrypt content to process it. There is no production
homomorphic encryption that lets a VLM "read" ciphertext.

**Why.** A stakeholder proposed encrypting screenshots before sending as
the privacy story. That is just HTTPS, which the API already does. Building
the legal posture on this would have been a problem.

**How to apply.** The real privacy lever for shipping pixels to Claude is a
**Zero-Data-Retention (ZDR)** agreement with Anthropic, which prevents
retention/logging of inputs and outputs. Never describe transport
encryption as a privacy guarantee against the API endpoint itself.

---

## L2 — Event-driven capture beats fixed-interval polling by 10–50×

**Rule.** Drive screenshots from native OS event hooks (mouse click on an
interactive element, window focus change, paste, save, form submit, URL
change), plus a slow heartbeat (~30 s) as a safety net. Do not screenshot
every N seconds blindly.

**Why.** Most adjacent frames at a 2–5 s polling rate are visually
identical or near-identical. Even simple frame-diffing throws ≥90% of them
away, so capturing them was wasted work, wasted disk, and wasted tokens
downstream. Event-driven capture lands on the frames that *matter*: the
ones where a state transition just occurred.

**How to apply.** When designing any "capture activity" component:
identify what the OS already knows happened, hook those signals, and only
sample pixels when one fires. Add a heartbeat to cover steady-state
Analyze actions where the user is reading but not clicking.

---

## L3 — Batch by session, not by clock

**Rule.** Send Claude one batch per ~30 min *session* (closed on idle gap
or context shift), not per day or per fixed window. Sessions are detected
locally with simple heuristics: idle > 2 min, or app/task context fully
changed.

**Why.** A procurement task (find vendor → check budget → raise PO →
approve) lives in a 10–30 min window. Daily batches blow past the
context window and have a same-day feedback loop. Shorter batches sacrifice
the cross-action context the model needs to segment workflows.

**How to apply.** Whenever you are tempted to batch by wall-clock,
ask "what is the natural unit of the work I'm labeling?" and batch by
that. For procurement: session. For something else: maybe document, task,
or trace.

---

## L4 — Two-pass labeling keeps each call sharp

**Rule.** Pass A: segment the session into action units (text-heavy,
downsampled images). Pass B: classify each action unit under CAGE
(per-unit, with all relevant frames). Do not try to segment AND classify
in one call.

**Why.** Combined calls do both tasks worse, fill the context with
material that is only relevant to one of the two jobs, and force a
re-segmentation every time we tune the taxonomy.

**How to apply.** Cache Pass A output. Re-run Pass B when the taxonomy
changes. Treat them as independent prompts versioned separately.

---

## L5 — Target 500 K tokens, not 1 M

**Rule.** Anthropic's 1 M context window degrades in answer quality past
roughly 500–600 K tokens. Plan batches at a ~500 K budget.

**Why.** "Fits in the window" is not the same as "the model still answers
well." Long-context degradation is empirically observed across providers.

**How to apply.** The batch builder must enforce the budget by selecting
or downsampling images (each ~1.5 K tokens) — not by hoping things fit.

---

## L6 — Validate the taxonomy on human-labeled hours before trusting model output

**Rule.** Before treating CAGE labels as load-bearing for cost estimation,
have ~2 humans independently label 10–20 hours of sessions. If inter-rater
agreement is low, the taxonomy is the bug, not the model.

**Why.** If humans can't agree on whether a minute is Analyze vs. Extract,
a model definitely will not, and every downstream cost number is built on
sand. This is the cheapest derisking step in the project.

**How to apply.** Phase 0 of the project plan. Do this before
investing in the Claude labeler at all if possible — even Pass A
segmentation benefits from a clear taxonomy.

---

## L8 — Prior art: task-mining is a mature commercial space; OSS is thin

**Rule.** This is not a green field. Several major commercial tools already
do "capture desktop activity + extract workflows", primarily targeted at
RPA-automation discovery. Our differentiation is the *VLM interpretation +
agent-token cost estimation* angle, not the capture itself.

**Why.** Web search (2026-05-19) surfaced:

- **UiPath Task Mining** — desktop event capture (clicks/keystrokes/app
  switches) → task flow diagrams. Closest direct analog.
- **Celonis** — process mining (system logs) + task mining hybrid.
- **ABBYY Timeline** — DOM/COM + image-based recording via browser
  plugins. Most architecturally similar to ours (hybrid pixel + structured
  approach).
- **Microsoft Power Automate Process Advisor** — uses a "Process Discovery
  Agent" but operates on documentation, not screen capture.
- **KYP.ai** and others — enterprise process-intelligence SaaS.

No directly-replaceable open-source tool exists; the space is dominated by
enterprise SaaS pricing ($10K+/year per bot for UiPath).

**How to apply.** Frame the project as differentiated on three axes that
incumbents don't own:
1. **VLM-driven semantic interpretation** of screens instead of relying on
   accessibility-tree scraping (which fails on legacy ERP UIs).
2. **Agent-token cost estimation** as the primary output, not just
   "automation candidates" — none of the incumbents price LLM-agent
   replacement cost.
3. **Privacy-local by design** — raw screen data never leaves the machine.
   Most commercial tools require uploading recordings to vendor cloud.

Worth glancing at ABBYY Timeline's hybrid recording model and UiPath's
trigger-based event capture before locking architectural decisions — they
likely already solved the obvious mistakes.

---

## L9 — WSL2 port-forwarding bleeds local servers to Windows

**Rule.** A process bound to `localhost:PORT` inside WSL2 is reachable
from the Windows host's browser at the same `localhost:PORT`. If a
Windows process also tries to bind that port, the result is
unpredictable — the browser might hit either one.

**Why.** During this PoC, a WSL demo server on `:8765` was answering the
user's Windows browser requests for ~30 min while their Windows daemon
was running on the same port. Symptoms: the page showed "demo" data the
user didn't recognize, the Daemon status badge said "no status file"
(because demo data has no `_status.json`).

**How to apply.** Whenever running an HTTP server in WSL for testing,
kill it before the user starts their Windows-side equivalent. Or use
non-overlapping ports.

---

## L10 — Chrome ignores meta-refresh + no-store on localhost

**Rule.** `<meta http-equiv="refresh">` plus `Cache-Control: no-store` is
NOT sufficient to make a localhost page reliably reload with fresh
content. The page may serve stale HTML, tabs flicker mid-click,
hash-based tab state gets reset unpredictably.

**Why.** Several browsers (notably Chrome) have aggressive caching
heuristics for localhost that ignore standard cache headers.

**How to apply.** For "live" pages on localhost, use **fetch-polling**
instead: serve a small stable shell HTML and a separate `data.json`;
poll `data.json` via `fetch()` with `cache: 'no-store'` and a cache-bust
`?_t=Date.now()` query param. Update DOM in place. The page never
reloads, so tab state, scroll position, etc. all persist trivially.

---

## L11 — Anthropic vision API has stricter limits than the docs suggest

**Rule.** Real limits encountered in this PoC:

- Total request: 32 MB
- Per-image raw size: 5 MB
- **Per-image dimension for "multi-image requests": 2000 px per side**
  (undocumented as of our session)
- Single-image requests appear to allow up to 8000×8000 px per docs
- Max 100 images per request per docs

**Why.** We hit each limit in turn:

- 413 Payload Too Large at ~100 MB total
- `image exceeds 5 MB maximum: 14582212 bytes`
- `At least one of the image dimensions exceed max allowed size for many-image requests: 2000 pixels`

**How to apply.** Keep chunks small (≤ 6 images per request was empirically
fine) so the 2000 px multi-image rule doesn't trigger. Compress images
in this order to minimize pixel loss: (a) leave raw PNG ≤ 5 MB
untouched, (b) JPEG at original dimensions, decreasing quality, (c)
last resort, resize.

---

## L12 — The workflow graph IS the artifact, not per-session labels

**Rule.** The output of the project is a directed graph where each unique
*action* is one node (with CAGE label, observation count, token estimate).
Repeated instances across sessions collapse into the same node. Per-session
label dumps are intermediate noise.

**Why.** User feedback partway through: "we want a mapping of resources,
action goal and starting point — the agent doesn't have to do exact same
actions, it just needs to understand the problem, the resources, and the
final goal." Per-screenshot classification was the wrong target.

**How to apply.** The workflow has *both* a graph (evidence: nodes,
edges, observation counts, token estimates) AND a goal-oriented summary
(goal / resources / trigger fields). The summary is the product output
for an automating agent; the graph is the backing evidence that makes
the cost estimate defensible.

---

## L13 — Per-action workflow routing, not per-session

**Rule.** Route each cognitive *action* to a workflow independently —
existing workflow, new workflow, or noise. A single session can
contribute observations to multiple workflows simultaneously.

**Why.** Real employee sessions interleave tasks: vendor research →
Slack interruption → invoice question → back to vendor research. Forcing
a session-level routing decision either over-merges or loses signal.

**How to apply.** Output schema gives each action its own
`target_workflow_kind = existing | new | noise`. Noise actions are
counted but not stored as observations. Multi-workflow sessions get the
workflow store touched in multiple places per Claude call.

---

## L14 — Stability detector is a cheap HITL trigger

**Rule.** A workflow is "ready for human review" when N consecutive
update calls (default 20) add no new node or edge — only increment
observation counts on existing ones. The structure has converged.

**Why.** You don't want to require human review on every call (too
expensive). You also don't want to ship an unreviewed workflow as
"complete" (too risky). The stability count is the cheap heuristic
that says "this workflow has settled".

**How to apply.** Workflow gains `stable_observations` and
`stability_threshold`. Increment on structurally-noop calls, reset on
structural changes. When ≥ threshold, set `is_complete=True` and queue
for HITL.

---

## L15 — Browser-side page weight is a hidden tab-killer

**Rule.** A 134 MB self-contained HTML (full PNGs base64-inlined) will
*appear* to break tabs, freeze the page, and ignore clicks — even when
the JS is syntactically valid and the renderer is updating the file.
Chrome silently struggles to parse multi-hundred-MB inline scripts.

**Why.** During this PoC the user spent significant time thinking the
viz was broken; root cause was inlined base64 image weight. Symptom of
"tabs not clickable" is much more often "page too heavy to be interactive"
than "JS error".

**How to apply.** Always split data into a separate JSON file the page
fetches lazily. Inline only what's tiny (event metadata + thumbnails).
Serve full-resolution images via a route the browser pulls on demand
(`/screens/<path>`). Target ~20-50 KB for the shell HTML.

---

## L16 — Real cost data for PoC scale

**Rule.** Per Opus 4.7 pricing (~$15/M input, $75/M output) the labeler
costs about **$0.80 per 10-minute employee session**. Linear
extrapolation: **~$30/employee/workday** at scale.

**Why.** Calibration data from our actual run: ~30K input + ~4K output
tokens for a 64-event procurement session ≈ $0.80. Multiple noise-only
sessions cost ~$0.50 each (system prompt overhead dominates).

**How to apply.** PoC scale is fine to leave at full quality. For
production with many employees × full days, the cost trade-offs to
revisit are: (a) JPEG compression of screenshots before sending, (b)
caching the workflow directory in the system prompt with Anthropic's
prompt-caching feature, (c) sending only event metadata + a few
representative screenshots per session instead of all frames.

---

## L7 — "What is PII vs. what is signal" is a project-viability question

**Rule.** Vendor names, PO numbers, GL codes, dollar amounts are the
*entire signal* for procurement workflow analysis. Redact them and you
have no useful data. Keep them and you have widened the data perimeter.

**Why.** The redaction list determines whether the system produces
anything useful at all. It is a legal + client conversation, not an
engineering decision.

**How to apply.** Get the redaction list signed off before serious code
investment. The redactor in `enrich/` should treat the list as
configuration, not constants, so we can change it without a code change.

---

## L17 — Real cost anatomy from a 2-hour live run (2026-06-09)

**Rule.** Cost tracks **activity density, not wall-clock** — each kept frame
is ~1,500 input tokens, so a busy hour costs more than two idle ones. A 2-h
all-noise run: 66 frames, 16 sessions, 20 calls, **$0.62** on Sonnet 4.6
(~$0.04/session); a busier hour hit ~$1.60. Both far under the $30/hr guard.
Variance is benign and proportional to how much the user did.

**Why (the input-token breakdown, 146K in / 12K out):**
- **Images ≈ 60%.** Screenshots were 4K (3840×2160), but **4K does not cost
  extra tokens** — Anthropic auto-scales any image to ~1568 px / ~1.15 MP
  *before* billing, so 4K and 1568 px cost the same ~1,500 tokens. 4K only
  wastes upload. The real image-token lever is downscaling **below** 1568 px
  (now the `--max-image-px` knob; 1568 is free, 1280≈−20%, with a legibility
  floor for small text — A/B it, don't guess).
- **Repeated overhead ≈ 40%.** System prompt + workflow directory + event-log
  are re-sent every call (~2,900 tok floor × 20 calls). This is what prompt
  caching attacks once the directory grows, and what trimming per-call
  verbosity attacks now.
- **Output is ~8% of tokens but ~29% of *cost*** (output is 5× input price), so
  trimming verbose JSON (`rationale`, etc.) is an outsized, easy lever.

**Also:** on pure noise the labeler correctly tagged everything `noise` (no
hallucinated procurement workflows) — good precision signal. NB: L16's
$15/$75 Opus figure is stale Claude-3 pricing; Opus 4.7 is $5/$25, Sonnet 4.6
$3/$15. Every run now writes a `runs/run_<stamp>.json` + appends `runs.jsonl`
(see `analytics/metrics.py`) so cost/keep-rate/noise-ratio are comparable
across config changes.

**How to apply.** Cost-reduction order, cheapest first: (1) fewer frames
(dedup — ring buffer + window-focus gate + click-settle, all just shipped),
(2) trim output JSON verbosity, (3) trim/cache per-call overhead, (4)
`--max-image-px` below 1568 with a legibility check. Measure each against
`runs.jsonl`, not intuition.
