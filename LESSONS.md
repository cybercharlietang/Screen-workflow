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
