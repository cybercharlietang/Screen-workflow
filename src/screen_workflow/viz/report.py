"""Render a self-contained HTML report from a Store.

The report is one ``index.html`` file with images inlined as base64
data URIs and JSON inlined in ``<script>`` blocks. No server needed.
Double-click in a file manager to open in the browser.
"""

from __future__ import annotations

import base64
import html
import io
import json
import logging
from datetime import datetime
from pathlib import Path

from PIL import Image

from screen_workflow.schemas import Event, Observation, Session, Workflow
from screen_workflow.storage.db import Store

log = logging.getLogger(__name__)

# Visualizer cap: too many full-res screenshots crash the browser. We render
# the N most-recent events with thumbnails. The full DB still has everything.
MAX_EVENTS_RENDERED = 100
THUMBNAIL_MAX_PX = 480
THUMBNAIL_JPEG_QUALITY = 60


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _img_data_uri(path: Path) -> str:
    """Encode the screenshot as a thumbnail JPEG data URI.

    Resizes longest edge to ``THUMBNAIL_MAX_PX`` and encodes JPEG at
    ``THUMBNAIL_JPEG_QUALITY``. Cuts a typical 1080p PNG (~1.5 MB) down to
    20–60 KB so the page stays under 10 MB even with 100 events.
    """
    if not path.exists():
        return ""
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((THUMBNAIL_MAX_PX, THUMBNAIL_MAX_PX), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=THUMBNAIL_JPEG_QUALITY, optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
    except Exception:  # noqa: BLE001
        log.exception("thumbnail failed for %s; skipping", path)
        return ""


def _event_dict(e: Event, screens_root: Path) -> dict:
    return {
        "event_id": e.event_id,
        "ts": _ts(e.ts),
        "session_id": e.session_id,
        "app": e.app,
        "window_title": e.window_title,
        "url": e.url,
        "trigger": e.trigger.type.value,
        "target_label": e.trigger.target_label,
        "ocr_text": e.ocr_text,
        "ui_elements": [u.model_dump() for u in e.ui_elements],
        "screenshot": _img_data_uri(screens_root / e.screenshot_path),
    }


def _session_dict(s: Session) -> dict:
    return {
        "session_id": s.session_id,
        "start_ts": _ts(s.start_ts),
        "end_ts": _ts(s.end_ts),
        "close_reason": s.close_reason.value,
        "event_ids": s.event_ids,
    }


def _observation_dict(o: Observation, node_lookup: dict[str, dict]) -> dict:
    node = node_lookup.get(o.node_id, {})
    return {
        "observation_id": o.observation_id,
        "workflow_id": o.workflow_id,
        "session_id": o.session_id,
        "node_id": o.node_id,
        "node_name": node.get("canonical_name", o.node_id),
        "cage_label": node.get("cage_label", "?"),
        "system": node.get("system", ""),
        "evidence_frame_ids": o.evidence_frame_ids,
        "confidence": o.confidence,
        "observed_at": _ts(o.observed_at),
    }


def _workflow_dict(w: Workflow) -> dict:
    return {
        "workflow_id": w.workflow_id,
        "name": w.name,
        "goal": w.goal,
        "resources": w.resources,
        "trigger": w.trigger,
        "noise_actions_count": w.noise_actions_count,
        "is_complete": w.is_complete,
        "stable_observations": w.stable_observations,
        "stability_threshold": w.stability_threshold,
        "sessions_processed": w.sessions_processed,
        "nodes": [
            {
                "node_id": n.node_id,
                "canonical_name": n.canonical_name,
                "cage_label": n.cage_label.value,
                "system": n.system,
                "data_object_pattern": n.data_object_pattern,
                "estimated_tokens": n.estimated_tokens,
                "expected_agent_steps": n.expected_agent_steps,
                "observation_count": n.observation_count,
                "confidence": n.confidence,
                "rationale": n.rationale,
            }
            for n in w.nodes.values()
        ],
        "edges": [
            {
                "from_node": e.from_node,
                "to_node": e.to_node,
                "observation_count": e.observation_count,
            }
            for e in w.edges
        ],
        "created_at": _ts(w.created_at),
        "last_updated_at": _ts(w.last_updated_at),
    }


def _read_status(store_root: Path) -> dict:
    """Read the daemon's status file if present."""
    p = store_root / "_status.json"
    if not p.exists():
        return {"present": False}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d["present"] = True
        return d
    except Exception:  # noqa: BLE001
        return {"present": False}


def _read_audit_logs(store_root: Path) -> list[dict]:
    """All per-session labeler audit logs, newest first."""
    audit_dir = store_root / "audit"
    if not audit_dir.exists():
        return []
    out = []
    for p in sorted(audit_dir.glob("*.json"), reverse=True):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return out


def render(
    store: Store,
    out_dir: Path,
    session_id: str | None = None,
    auto_refresh: bool = True,
) -> Path:
    """Write a self-contained HTML report to ``out_dir/index.html``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    all_events = list(store.iter_events(session_id))
    total_events = len(all_events)
    # Cap to most-recent N to keep the HTML page responsive.
    if total_events > MAX_EVENTS_RENDERED:
        all_events = all_events[-MAX_EVENTS_RENDERED:]
    events = [_event_dict(e, store.screens_dir) for e in all_events]
    sessions = [_session_dict(s) for s in store.iter_sessions()]
    workflows = [_workflow_dict(w) for w in store.iter_workflows()]
    node_lookup = {
        n["node_id"]: n for w in workflows for n in w["nodes"]
    }
    observations = [
        _observation_dict(o, node_lookup) for o in store.iter_observations(session_id=session_id)
    ]

    # Per-workflow cost rollup: agent cost to replace one execution of the workflow
    # ≈ sum over nodes of estimated_tokens × expected_agent_steps
    cost_summary = []
    for wf in workflows:
        nodes = wf["nodes"]
        per_execution = sum(n["estimated_tokens"] * n["expected_agent_steps"] for n in nodes)
        cage_breakdown: dict[str, int] = {}
        for n in nodes:
            cage_breakdown[n["cage_label"]] = (
                cage_breakdown.get(n["cage_label"], 0)
                + n["estimated_tokens"] * n["expected_agent_steps"]
            )
        cost_summary.append(
            {
                "workflow_id": wf["workflow_id"],
                "name": wf["name"],
                "is_complete": wf["is_complete"],
                "node_count": len(nodes),
                "per_execution_tokens": per_execution,
                "cage_breakdown": cage_breakdown,
            }
        )

    payload = {
        "generated_at": _ts(datetime.now()),
        "session_filter": session_id,
        "events": events,
        "events_total_in_db": total_events,
        "events_rendered_cap": MAX_EVENTS_RENDERED,
        "sessions": sessions,
        "workflows": workflows,
        "observations": observations,
        "cost_summary": cost_summary,
        "daemon_status": _read_status(store.root),
        "audit_logs": _read_audit_logs(store.root),
    }
    payload_json = json.dumps(payload, default=str)

    # Always write data.json so the page can poll it without a full reload.
    (out_dir / "data.json").write_text(payload_json, encoding="utf-8")

    # The page itself is small and stable; the JS fetches data.json on a
    # timer and updates the DOM in place. Tabs persist, no flicker, no
    # cache-bust hacks.
    html_out = (
        _TEMPLATE.replace("__TITLE__",
            html.escape(f"Screen-workflow report — {session_id or 'all sessions'}"))
        .replace("__AUTO_REFRESH__", "")
    )

    out_path = out_dir / "index.html"
    out_path.write_text(html_out, encoding="utf-8")
    return out_path


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
__AUTO_REFRESH__
<title>__TITLE__</title>
<style>
  body { font: 14px/1.4 system-ui, sans-serif; margin: 0; color: #222; background: #fafafa; }
  header { padding: 12px 20px; background: #1a1a1a; color: #fff; display: flex; align-items: center; gap: 16px; }
  header h1 { margin: 0; font-size: 16px; font-weight: 500; flex: 1; }
  header .meta { font-size: 12px; color: #aaa; margin-top: 4px; }
  header .status-card { background: #2a2a32; border-radius: 6px; padding: 8px 12px; font-size: 12px; min-width: 280px; }
  header .status-card .row { display: flex; justify-content: space-between; gap: 12px; }
  header .status-card .row + .row { margin-top: 2px; }
  header .status-card .label { color: #888; }
  header .status-card .value { color: #fff; font-variant-numeric: tabular-nums; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
  .badge-ok { background: #1f7a4d; color: #fff; }
  .badge-warn { background: #d97706; color: #fff; }
  .badge-bad { background: #b91c1c; color: #fff; }
  nav { display: flex; gap: 0; background: #fff; border-bottom: 1px solid #ddd; padding: 0 20px; }
  nav button { background: none; border: none; padding: 12px 16px; cursor: pointer; font-size: 14px; color: #555; border-bottom: 2px solid transparent; }
  nav button.active { color: #1a1a1a; border-bottom-color: #1a1a1a; font-weight: 500; }
  main { padding: 20px; }
  section { display: none; }
  section.active { display: block; }
  table { border-collapse: collapse; width: 100%; background: #fff; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; font-size: 13px; vertical-align: top; }
  th { background: #f4f4f4; font-weight: 600; }
  tr.event-row { cursor: pointer; }
  tr.event-row:hover { background: #f9f9f9; }
  tr.event-row.selected { background: #fff8dc; }
  .detail { display: flex; gap: 20px; margin-top: 16px; }
  .detail img { max-width: 60%; border: 1px solid #ddd; background: #fff; }
  .detail .meta { flex: 1; font-size: 13px; }
  .detail .meta dt { font-weight: 600; margin-top: 8px; color: #555; }
  .detail .meta dd { margin: 0; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; color: #fff; }
  .pill-C { background: #1f7a4d; }
  .pill-A { background: #1f5fa7; }
  .pill-G { background: #a13a8c; }
  .pill-E { background: #d97706; }
  .pill-heartbeat { background: #888; }
  .pill-click { background: #2c7; }
  .pill-window_focus { background: #57c; }
  .pill-paste, .pill-save, .pill-submit, .pill-file_open, .pill-file_save, .pill-url_change { background: #999; }
  .empty { color: #888; font-style: italic; padding: 12px 0; }
  .num { font-variant-numeric: tabular-nums; text-align: right; }
</style>
</head>
<body>
<header>
  <div style="flex:1">
    <h1>__TITLE__</h1>
    <div class="meta" id="meta-line"></div>
  </div>
  <div class="status-card" id="status-card">
    <div class="row"><span class="label">Daemon</span><span class="value" id="status-daemon">—</span></div>
    <div class="row"><span class="label">Listeners</span><span class="value" id="status-listeners">—</span></div>
    <div class="row"><span class="label">Events captured</span><span class="value" id="status-events">—</span></div>
    <div class="row"><span class="label">Last event</span><span class="value" id="status-last">—</span></div>
  </div>
</header>
<nav>
  <button class="tab-btn active" data-tab="events">Events</button>
  <button class="tab-btn" data-tab="sessions">Sessions</button>
  <button class="tab-btn" data-tab="workflows">Workflows</button>
  <button class="tab-btn" data-tab="observations">Observations</button>
  <button class="tab-btn" data-tab="pipeline">Pipeline</button>
  <button class="tab-btn" data-tab="cost">Cost</button>
</nav>
<main>
  <section id="events" class="active">
    <table>
      <thead><tr><th>#</th><th>Time</th><th>App</th><th>Window</th><th>Trigger</th><th>Session</th></tr></thead>
      <tbody id="events-tbody"></tbody>
    </table>
    <div id="event-detail"></div>
  </section>
  <section id="sessions">
    <table>
      <thead><tr><th>Session</th><th>Start</th><th>End</th><th>Reason</th><th class="num">Events</th></tr></thead>
      <tbody id="sessions-tbody"></tbody>
    </table>
  </section>
  <section id="workflows">
    <div id="workflows-content"></div>
  </section>
  <section id="observations">
    <table>
      <thead><tr><th>Time</th><th>Workflow Node</th><th>CAGE</th><th>System</th><th>Session</th><th class="num">Conf.</th><th>Frames</th></tr></thead>
      <tbody id="observations-tbody"></tbody>
    </table>
  </section>
  <section id="pipeline">
    <div id="pipeline-content"></div>
  </section>
  <section id="cost">
    <div id="cost-content"></div>
  </section>
</main>
<script>
const POLL_INTERVAL_MS = 3000;
let currentData = null;

async function fetchData() {
  try {
    const r = await fetch('data.json?_t=' + Date.now(), { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch (e) {
    console.warn('fetch failed', e);
    return null;
  }
}

function renderAll(data) {
  currentData = data;
  render(data);
}

(function () {
  // Set up tabs ONCE (before any data — they work regardless).
  function activateTab(name) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('section').forEach(s => s.classList.remove('active'));
    const btn = document.querySelector('.tab-btn[data-tab="' + name + '"]');
    const sec = document.getElementById(name);
    if (btn && sec) { btn.classList.add('active'); sec.classList.add('active'); }
  }
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const t = btn.dataset.tab;
      window.location.hash = '#' + t;
      activateTab(t);
    });
  });
  activateTab((window.location.hash || '#events').slice(1));

  // Kick off polling.
  (async () => {
    const data = await fetchData();
    if (data) renderAll(data);
    setInterval(async () => {
      const newData = await fetchData();
      if (newData) renderAll(newData);
    }, POLL_INTERVAL_MS);
  })();
})();

function render(data) {
  const totalInDb = data.events_total_in_db || data.events.length;
  const eventsCountMsg = (totalInDb > data.events.length)
    ? `${data.events.length} of ${totalInDb} events shown (most recent)`
    : `${data.events.length} events`;
  document.getElementById('meta-line').textContent =
    `Generated ${data.generated_at} — ${eventsCountMsg}, ${data.sessions.length} sessions, ${data.workflows.length} workflows`;

  // Daemon status
  (function () {
    const s = data.daemon_status || {present: false};
    const d = document.getElementById('status-daemon');
    const l = document.getElementById('status-listeners');
    const ev = document.getElementById('status-events');
    const last = document.getElementById('status-last');

    if (!s.present) {
      d.innerHTML = '<span class="badge badge-bad">no status file</span>';
      l.textContent = '—'; ev.textContent = '—'; last.textContent = '—';
      return;
    }
    // Daemon alive?
    let alive = !!s.alive;
    // staleness: heartbeat older than 8s = stale
    let stale = false;
    if (s.heartbeat_ts) {
      const hb = new Date(s.heartbeat_ts).getTime();
      const age = (Date.now() - hb) / 1000;
      if (age > 8) stale = true;
    }
    if (alive && !stale) {
      d.innerHTML = '<span class="badge badge-ok">alive</span>';
    } else if (alive && stale) {
      d.innerHTML = '<span class="badge badge-warn">stale</span>';
    } else {
      d.innerHTML = '<span class="badge badge-bad">stopped</span>';
    }
    if (s.listeners_ok === true) {
      l.innerHTML = '<span class="badge badge-ok">ok</span>';
    } else if (s.listeners_ok === false) {
      l.innerHTML = '<span class="badge badge-bad">failed</span>';
    } else {
      l.textContent = '—';
    }
    ev.textContent = s.events_captured != null ? s.events_captured : '—';
    last.textContent = s.last_event_ts ? s.last_event_ts.substring(11, 19) + ' UTC' : 'never';
    if (s.last_error) {
      const errDiv = document.createElement('div');
      errDiv.style.cssText = 'background:#b91c1c;color:#fff;padding:6px 10px;font-size:12px;';
      errDiv.textContent = 'Daemon error: ' + s.last_error;
      document.body.insertBefore(errDiv, document.body.firstChild.nextSibling);
    }
  })();

  // Clear all dynamic regions so repeated polls don't accumulate duplicates.
  ['events-tbody','sessions-tbody','observations-tbody',
   'workflows-content','pipeline-content','cost-content','event-detail'
  ].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = '';
  });

  // Events
  const tbody = document.getElementById('events-tbody');
  data.events.forEach((e, i) => {
    const tr = document.createElement('tr');
    tr.className = 'event-row';
    tr.dataset.idx = i;
    tr.innerHTML = `
      <td class="num">${i + 1}</td>
      <td>${escapeHtml(e.ts)}</td>
      <td>${escapeHtml(e.app)}</td>
      <td>${escapeHtml(e.window_title)}</td>
      <td><span class="pill pill-${e.trigger}">${e.trigger}</span></td>
      <td>${escapeHtml(e.session_id || '—')}</td>
    `;
    tr.addEventListener('click', () => showEventDetail(i, tr));
    tbody.appendChild(tr);
  });
  if (data.events.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No events captured yet.</td></tr>';
  }

  function showEventDetail(i, row) {
    document.querySelectorAll('tr.event-row').forEach(r => r.classList.remove('selected'));
    row.classList.add('selected');
    const e = data.events[i];
    const det = document.getElementById('event-detail');
    det.innerHTML = `
      <div class="detail">
        <img src="${e.screenshot || ''}" alt="screenshot" />
        <div class="meta">
          <dl>
            <dt>event_id</dt><dd>${escapeHtml(e.event_id)}</dd>
            <dt>ts</dt><dd>${escapeHtml(e.ts)}</dd>
            <dt>app</dt><dd>${escapeHtml(e.app)}</dd>
            <dt>window_title</dt><dd>${escapeHtml(e.window_title)}</dd>
            <dt>url</dt><dd>${escapeHtml(e.url || '—')}</dd>
            <dt>trigger</dt><dd>${escapeHtml(e.trigger)} (${escapeHtml(e.target_label || '—')})</dd>
            <dt>session</dt><dd>${escapeHtml(e.session_id || '—')}</dd>
            <dt>ocr_text</dt><dd><pre style="white-space:pre-wrap;margin:0">${escapeHtml(e.ocr_text || '—')}</pre></dd>
            <dt>ui_elements</dt><dd><pre style="white-space:pre-wrap;margin:0">${escapeHtml(JSON.stringify(e.ui_elements, null, 2))}</pre></dd>
          </dl>
        </div>
      </div>
    `;
  }

  // Sessions
  const stb = document.getElementById('sessions-tbody');
  data.sessions.forEach(s => {
    stb.innerHTML += `
      <tr>
        <td>${escapeHtml(s.session_id)}</td>
        <td>${escapeHtml(s.start_ts)}</td>
        <td>${escapeHtml(s.end_ts)}</td>
        <td>${escapeHtml(s.close_reason)}</td>
        <td class="num">${s.event_ids.length}</td>
      </tr>`;
  });
  if (data.sessions.length === 0) stb.innerHTML = '<tr><td colspan="5" class="empty">No sessions yet.</td></tr>';

  // Workflows
  const wfc = document.getElementById('workflows-content');
  if (!data.workflows || data.workflows.length === 0) {
    wfc.innerHTML = '<p class="empty">No workflows yet. Run <code>screen-workflow-label --workflow &lt;name&gt;</code> to start one.</p>';
  } else {
    data.workflows.forEach(w => {
      const stableBadge = w.is_complete
        ? '<span class="badge badge-ok">complete</span>'
        : `<span class="badge badge-warn">${w.stable_observations}/${w.stability_threshold} stable</span>`;
      const resourcesHtml = (w.resources && w.resources.length)
        ? w.resources.map(r => `<span class="badge" style="background:#e5e7eb;color:#444">${escapeHtml(r)}</span>`).join(' ')
        : '<span style="color:#888">—</span>';
      let html = `
        <div style="background:#fff; border:1px solid #ddd; border-radius:6px; padding:16px; margin-bottom:16px">
          <h2 style="margin:0 0 6px 0; font-size:16px">${escapeHtml(w.name)} ${stableBadge}</h2>
          <div style="background:#fafafa; border-left:3px solid #1f5fa7; padding:8px 12px; margin:8px 0 12px 0; font-size:13px">
            <div><span style="color:#666; font-weight:600">Goal:</span> ${escapeHtml(w.goal || '(not yet identified)')}</div>
            <div style="margin-top:4px"><span style="color:#666; font-weight:600">Trigger:</span> ${escapeHtml(w.trigger || '(not yet identified)')}</div>
            <div style="margin-top:4px"><span style="color:#666; font-weight:600">Resources:</span> ${resourcesHtml}</div>
          </div>
          <div style="font-size:12px; color:#666; margin-bottom:12px">
            ${w.nodes.length} nodes · ${w.edges.length} edges · sessions: ${w.sessions_processed.length}
            ${w.noise_actions_count > 0 ? ` · noise dropped: ${w.noise_actions_count}` : ''}
          </div>
          <table style="margin-bottom:8px">
            <thead><tr><th>Node</th><th>CAGE</th><th>System</th><th>Data object</th><th class="num">Tokens (mean)</th><th class="num">Steps</th><th class="num">Seen</th><th>Why</th></tr></thead>
            <tbody>`;
      w.nodes.forEach(n => {
        html += `
          <tr>
            <td>${escapeHtml(n.canonical_name)}</td>
            <td><span class="pill pill-${n.cage_label}">${n.cage_label}</span></td>
            <td>${escapeHtml(n.system)}</td>
            <td>${escapeHtml(n.data_object_pattern)}</td>
            <td class="num">${n.estimated_tokens.toLocaleString()}</td>
            <td class="num">${n.expected_agent_steps}</td>
            <td class="num">${n.observation_count}</td>
            <td style="font-size:12px; color:#555">${escapeHtml(n.rationale)}</td>
          </tr>`;
      });
      html += `
            </tbody>
          </table>`;
      if (w.edges.length > 0) {
        html += `<details><summary style="cursor:pointer; font-size:12px; color:#666">Transitions (${w.edges.length})</summary><div style="margin-top:8px; font-size:12px">`;
        w.edges.forEach(e => {
          html += `<div>${escapeHtml(e.from_node)} → ${escapeHtml(e.to_node)} <span style="color:#888">×${e.observation_count}</span></div>`;
        });
        html += `</div></details>`;
      }
      html += `</div>`;
      wfc.innerHTML += html;
    });
  }

  // Observations
  const otb = document.getElementById('observations-tbody');
  data.observations.forEach(o => {
    otb.innerHTML += `
      <tr>
        <td>${escapeHtml(o.observed_at)}</td>
        <td>${escapeHtml(o.node_name)}</td>
        <td><span class="pill pill-${o.cage_label}">${o.cage_label}</span></td>
        <td>${escapeHtml(o.system)}</td>
        <td>${escapeHtml(o.session_id)}</td>
        <td class="num">${o.confidence.toFixed(2)}</td>
        <td>${o.evidence_frame_ids.length}</td>
      </tr>`;
  });
  if (data.observations.length === 0) otb.innerHTML = '<tr><td colspan="7" class="empty">No observations yet.</td></tr>';

  // Pipeline — per-session diagnostic view
  const pip = document.getElementById('pipeline-content');
  const sessionsById = {};
  data.sessions.forEach(s => { sessionsById[s.session_id] = s; });
  const auditBySession = {};
  (data.audit_logs || []).forEach(a => { auditBySession[a.session_id] = a; });
  const sessionsList = data.sessions.slice().reverse();
  if (sessionsList.length === 0) {
    pip.innerHTML = '<p class="empty">No sessions yet. Run the daemon and segmenter first.</p>';
  } else {
    let html = '';
    sessionsList.forEach(s => {
      const a = auditBySession[s.session_id];
      const eventsInSession = data.events.filter(e => e.session_id === s.session_id);
      let inner = `
        <div style="background:#fff; border:1px solid #ddd; border-radius:6px; padding:14px; margin-bottom:12px">
          <h3 style="margin:0 0 8px 0; font-size:14px; font-family: ui-monospace, monospace">${escapeHtml(s.session_id)}</h3>
          <div style="font-size:12px; color:#555; margin-bottom:10px">
            ${escapeHtml(s.start_ts)} → ${escapeHtml(s.end_ts)} · closed by <b>${escapeHtml(s.close_reason)}</b> · ${eventsInSession.length} events
          </div>`;
      if (a) {
        const sum = a.summary || {};
        const wfs = sum.workflows_touched || [];
        // Aggregate actions across all chunks
        const chunks = a.chunks || (a.claude_response ? [{claude_response: a.claude_response}] : []);
        const actions = chunks.flatMap(c => (c.claude_response && c.claude_response.actions) || []);
        const nonNoise = actions.filter(x => (x.target_workflow_kind || '').toLowerCase() !== 'noise');
        const noiseActs = actions.filter(x => (x.target_workflow_kind || '').toLowerCase() === 'noise');
        const totalImages = chunks.reduce((s,c) => s + ((c.batch && c.batch.selected_frame_ids) || []).length, 0);
        const cr = {actions, chunks_count: chunks.length};
        inner += `
          <div style="display:grid; grid-template-columns: repeat(4, 1fr); gap:8px; font-size:12px; margin-bottom:10px">
            <div style="background:#f4f4f4; padding:6px 10px; border-radius:4px">
              <div style="color:#666">Chunks / images</div>
              <div style="font-weight:600">${chunks.length} call(s) / ${totalImages} frames</div>
            </div>
            <div style="background:#f4f4f4; padding:6px 10px; border-radius:4px">
              <div style="color:#666">Total tokens (Claude)</div>
              <div style="font-weight:600">${((sum.total_input_tokens||0) + (sum.total_output_tokens||0)).toLocaleString()}</div>
            </div>
            <div style="background:#f4f4f4; padding:6px 10px; border-radius:4px">
              <div style="color:#666">Workflows touched</div>
              <div style="font-weight:600">${wfs.length}</div>
            </div>
            <div style="background:#f4f4f4; padding:6px 10px; border-radius:4px">
              <div style="color:#666">Actions / noise</div>
              <div style="font-weight:600">${nonNoise.length} / ${noiseActs.length}</div>
            </div>
          </div>`;
        inner += `<h4 style="margin:10px 0 4px 0; font-size:12px; color:#444">Claude routing decisions</h4>`;
        inner += `<table style="font-size:12px"><thead><tr><th>Action</th><th>Workflow</th><th>CAGE</th><th class="num">Tokens</th><th class="num">Steps</th><th class="num">Conf.</th><th>Why</th></tr></thead><tbody>`;
        actions.forEach(act => {
          const kind = (act.target_workflow_kind || '').toLowerCase();
          const wfLabel = kind === 'noise'
            ? '<span style="color:#b91c1c; font-weight:600">NOISE</span>'
            : (kind === 'new' ? `<span style="color:#1f5fa7">NEW: ${escapeHtml(act.target_workflow_name || '')}</span>`
                              : `<code>${escapeHtml(act.target_workflow_id || '?')}</code>`);
          inner += `<tr>
              <td>${escapeHtml(act.canonical_name || act.node_id || '?')}</td>
              <td>${wfLabel}</td>
              <td>${kind !== 'noise' ? '<span class="pill pill-' + (act.cage_label||'') + '">' + (act.cage_label||'') + '</span>' : ''}</td>
              <td class="num">${(act.estimated_tokens || 0).toLocaleString()}</td>
              <td class="num">${act.expected_agent_steps || ''}</td>
              <td class="num">${(act.confidence != null) ? Number(act.confidence).toFixed(2) : ''}</td>
              <td>${escapeHtml(act.rationale || '')}</td>
            </tr>`;
        });
        inner += `</tbody></table>`;
        inner += `<details style="margin-top:8px"><summary style="font-size:12px; color:#666; cursor:pointer">Raw Claude response JSON</summary>
                  <pre style="background:#f4f4f4; padding:8px; font-size:11px; overflow:auto">${escapeHtml(JSON.stringify(cr, null, 2))}</pre></details>`;
      } else {
        inner += `<div class="empty" style="font-size:12px">Not yet labeled. Run <code>screen-workflow-label</code> to process this session.</div>`;
      }
      inner += `</div>`;
      html += inner;
    });
    pip.innerHTML = html;
  }

  // Cost
  const ctc = document.getElementById('cost-content');
  if (!data.cost_summary || data.cost_summary.length === 0) {
    ctc.innerHTML = '<p class="empty">Cost rolls up per workflow once labels exist.</p>';
  } else {
    let html = '';
    data.cost_summary.forEach(w => {
      html += `
        <div style="background:#fff; border:1px solid #ddd; border-radius:6px; padding:16px; margin-bottom:16px">
          <h2 style="margin:0 0 8px 0; font-size:16px">${escapeHtml(w.name)}</h2>
          <div style="font-size:13px; color:#444; margin-bottom:8px">
            One execution of this workflow ≈ <b>${w.per_execution_tokens.toLocaleString()}</b> agent tokens
            across ${w.node_count} action(s).
          </div>
          <table>
            <thead><tr><th>CAGE</th><th class="num">Tokens per execution</th></tr></thead>
            <tbody>`;
      ['C','A','G','E'].forEach(k => {
        const v = w.cage_breakdown[k] || 0;
        if (v === 0) return;
        const names = {C:'Capture', A:'Analyze', G:'Generate', E:'Extract'};
        html += `<tr><td><span class="pill pill-${k}">${k}</span> ${names[k]}</td><td class="num">${v.toLocaleString()}</td></tr>`;
      });
      html += `</tbody></table></div>`;
    });
    ctc.innerHTML = html;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[c]);
  }
}
</script>
</body>
</html>
"""
