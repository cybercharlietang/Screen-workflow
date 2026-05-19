# CLAUDE.md — Context for AI coding assistants

This file is loaded automatically by Claude Code when working in this repo.
Keep it concise; it competes with the user's actual prompt for attention.

## What this project is

Screen-workflow is a procurement-workflow discovery tool. A Windows daemon
captures meaningful screen events on a client employee's machine, batches them
into ~30-minute sessions, and sends them to Claude for two-pass labeling under
the **CAGE** taxonomy (Capture / Analyze / Generate / Extract). Labeled
actions feed a cost model that estimates the token cost of automating each
workflow with an agent.

Full architecture in [SPEC.md](SPEC.md). Read it before editing pipeline code.

## What matters most

1. **Privacy boundary is load-bearing.** Raw screenshots and raw OCR text
   stay on the employee machine. They may transit to Claude under a
   Zero-Data-Retention (ZDR) agreement; they never persist in our cloud.
   Only labeled, redacted, structured JSON is exported. Do not introduce
   code paths that violate this without first updating SPEC.md § 3 and
   getting explicit user sign-off.
2. **CAGE taxonomy is provisional.** It will be refined against
   human-labeled sessions. Do not hard-code business logic that assumes
   the four labels are final. Keep label sets behind one config constant.
3. **Token budget is real.** Claude's 1 M context degrades past ~600 K.
   Batch builder targets 500 K and must enforce it; do not assume "fits in
   context" without counting.

## Architecture in one paragraph

Capture daemon (event hooks + heartbeat) → frame dedupe → local enricher
(OCR, UI Automation, redactor) → encrypted SQLite + screenshots dir →
session segmenter (30 min / idle / context shift) → batch builder (token
budget, image selection) → Claude labeler (Pass A segments, Pass B
classifies) → company analytics DB + HITL review queue.

Each stage produces an inspectable artifact. Build and validate stages in
order; do not skip ahead.

## Repo conventions

- Python 3.11+. One package, `screen_workflow`, under `src/`.
- Subpackages mirror the pipeline stages: `capture`, `enrich`, `session`,
  `labeler`, `storage`, `analytics`.
- Type-annotate public functions. Use `pydantic` models for cross-stage
  data contracts (events, sessions, labels) — these are the schemas the
  rest of the pipeline depends on; changing them is a breaking change.
- Tests in `tests/`, mirroring `src/` layout.
- No file > 400 lines without a reason — split into siblings instead.
- Comments only where the *why* is non-obvious. No "what" comments.

## Things to ask before changing

- The redaction list (what counts as PII vs. signal). Legal/client
  decision, not a code decision.
- The CAGE taxonomy itself. Update SPEC.md first, code second.
- The event/session/label schemas. Downstream analytics depend on them.
- Adding any network call from the daemon that goes anywhere other than
  the Claude API.

## Where to look

- [SPEC.md](SPEC.md) — pipeline detail, schemas, taxonomy, open questions.
- [LESSONS.md](LESSONS.md) — non-trivial lessons. Read before designing;
  append when you learn something the next session would benefit from.
- [TODOS.md](TODOS.md) — phase plan and open backlog. Update as you go.

## What not to do

- Do not implement cross-OS support yet. Windows-only for v1.
- Do not add "encryption before sending to Claude" as a privacy mechanism;
  it does not do what it sounds like. ZDR is the actual privacy lever.
- Do not capture at fixed intervals "for simplicity." Event-driven +
  heartbeat is the design and cuts data volume by 10–50×.
- Do not send each frame to Claude individually. Session-batched, two-pass.
- Do not add new top-level docs without asking; this set
  (SPEC/CLAUDE/LESSONS/TODOS/README) is the contract.
