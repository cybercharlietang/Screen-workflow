# Screen-workflow

Screen-activity capture and workflow extraction for procurement-process
discovery (proof of concept).

A local daemon on a client employee's Windows machine captures meaningful
screen events (driven by mouse/keyboard/focus/new-window hooks + a heartbeat,
not fixed-interval polling), dedupes frames, batches them into sessions, and
sends them to Claude, which builds an **emergent workflow graph** (actions =
nodes, transitions = edges) and classifies activity as a known/new workflow or
noise. The graph is the artifact; it's shaped so a computer-use agent could
replace a workflow, and it backs an estimate of the agent-token cost of
automating each one. (A coarse `cage_label` tag is retained per node, but the
taxonomy is no longer the primary output — see [LESSONS.md](LESSONS.md).)

Raw screenshots and OCR text **never leave the machine** except in transit to
the Claude API under ZDR. See [CLAUDE.md](CLAUDE.md).

## Status

Working end-to-end continuous pipeline: **capture → dedupe → segment → label
→ live viz → per-run metrics**, all in one process (`screen-workflow-live`).
Proven on multi-hour live runs; cheap (~$0.3–1.6/hr, activity-dependent) with
soft/hard cost guards. v1 runtime target: Windows 10/11. 116 tests passing.

## Quickstart (Windows)

```powershell
# 1. one-time setup
git clone https://github.com/cybercharlietang/Screen-workflow.git
cd Screen-workflow
.\setup.ps1

# 2. API key — put it in a gitignored .env (start.ps1 auto-loads it)
Set-Content -Path .\.env -Value "ANTHROPIC_API_KEY=sk-ant-..." -NoNewline

# 3. run a live session (or double-click start.bat)
.\start.ps1 -Seconds 3600 -Reset        # 1-hour clean run; viz at http://localhost:8765
```

Useful flags on `start.ps1`: `-HashMode perceptual|exact`,
`-MaxImagePx 1568` (lower cuts image tokens, costs legibility), `-ApiKey`.
Stop early with **Ctrl+C** (graceful). Each run writes a summary to
`runs/run_<stamp>.json` and appends `runs/runs.jsonl`.

Inspect / compare runs:

```powershell
screen-workflow-metrics --root .\local_data --runs .\runs
# (or without reinstalling: python -m screen_workflow.analytics.metrics ...)
```

## Architecture (pipeline stages = subpackages under `src/screen_workflow/`)

| Stage | Package | What it does |
|-------|---------|--------------|
| Capture | `capture/` | event hooks + heartbeat + new-window/dialog detection (`daemon.py`); meaningful-event filter (`filter.py`); frame dedupe, perceptual or exact (`dedupe.py`) |
| Segment | `session/` | rolling-flush segmenter (`segmenter.py`): close a session every 40 events / 5 min / idle gap |
| Label | `labeler/` | image selection + chunking (`batch.py`); Claude call + workflow-graph merge (`api.py`) |
| Store | `storage/` | SQLite events / sessions / workflows / observations / api_calls (`db.py`) |
| Cost | `cost_monitor.py` | per-call token tracking, hourly burn rate, soft/hard/total guards |
| Viz | `viz/` | live static-HTML report with cost panel (`report.py`) |
| Metrics | `analytics/` | per-run summary + cross-run table (`metrics.py`) |
| Orchestrate | `live.py` | runs all of the above concurrently |

## Documents

- **[SPEC.md](SPEC.md)** — pipeline spec (schemas, components, token budget).
- **[CLAUDE.md](CLAUDE.md)** — context + rules for AI assistants. Read first.
- **[LESSONS.md](LESSONS.md)** — non-trivial lessons. Append, don't rewrite.
- **[TODOS.md](TODOS.md)** — backlog, phase plan, and current next steps.
