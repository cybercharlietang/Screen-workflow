# Screen-workflow

Screen-activity capture and workflow extraction for procurement-process discovery.

A local daemon on a client employee's machine captures meaningful screen events
(driven by mouse/keyboard/focus hooks, not fixed-interval polling), enriches
them locally (OCR, accessibility tree, redaction), batches them into ~30-minute
sessions, and sends them to Claude for two-pass labeling under the **CAGE**
taxonomy (Capture / Analyze / Generate / Extract). Labeled actions feed a
downstream analytics store used to estimate the agent-token cost of automating
each workflow.

## Documents

- **[SPEC.md](SPEC.md)** — full pipeline specification (data schemas,
  components, token budget, CAGE taxonomy).
- **[CLAUDE.md](CLAUDE.md)** — context for AI coding assistants working in
  this repo. Review before changing.
- **[LESSONS.md](LESSONS.md)** — non-trivial lessons accumulated during the
  project. Append, do not rewrite.
- **[TODOS.md](TODOS.md)** — current backlog and phase plan.

## Status

Pre-implementation. Scaffolding only. v1 target: Windows.
