"""Render a self-contained HTML report from a Store.

The report is one ``index.html`` file with images inlined as base64
data URIs and JSON inlined in ``<script>`` blocks. No server needed.
Double-click in a file manager to open in the browser.
"""

from __future__ import annotations

import base64
import html
import json
from datetime import datetime
from pathlib import Path

from screen_workflow.schemas import Event, Label, Session
from screen_workflow.storage.db import Store


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _img_data_uri(path: Path) -> str:
    if not path.exists():
        return ""
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


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


def _label_dict(l: Label) -> dict:
    return {
        "action_id": l.action_id,
        "session_id": l.session_id,
        "cage_label": l.cage_label.value,
        "system": l.system,
        "data_object": l.data_object,
        "estimated_tokens": l.estimated_tokens,
        "start_ts": _ts(l.start_ts),
        "end_ts": _ts(l.end_ts),
        "evidence_frame_ids": l.evidence_frame_ids,
        "confidence": l.confidence,
        "rationale": l.rationale,
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


def render(
    store: Store,
    out_dir: Path,
    session_id: str | None = None,
    auto_refresh: bool = True,
) -> Path:
    """Write a self-contained HTML report to ``out_dir/index.html``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    events = [_event_dict(e, store.screens_dir) for e in store.iter_events(session_id)]
    sessions = [_session_dict(s) for s in store.iter_sessions()]
    labels = [_label_dict(l) for l in store.iter_labels(session_id)]

    cost_summary: dict[str, dict[str, float]] = {}
    for l in labels:
        bucket = cost_summary.setdefault(l["cage_label"], {"count": 0, "tokens": 0})
        bucket["count"] += 1
        bucket["tokens"] += l["estimated_tokens"]

    payload = {
        "generated_at": _ts(datetime.now()),
        "session_filter": session_id,
        "events": events,
        "sessions": sessions,
        "labels": labels,
        "cost_summary": cost_summary,
        "daemon_status": _read_status(store.root),
    }
    payload_json = json.dumps(payload, default=str)

    refresh_meta = '<meta http-equiv="refresh" content="3">' if auto_refresh else ""

    html_out = (
        _TEMPLATE.replace("__PAYLOAD_JSON__", payload_json)
        .replace(
            "__TITLE__",
            html.escape(f"Screen-workflow report — {session_id or 'all sessions'}"),
        )
        .replace("__AUTO_REFRESH__", refresh_meta)
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
  <button class="tab-btn" data-tab="labels">Labels</button>
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
  <section id="labels">
    <table>
      <thead><tr><th>Action</th><th>CAGE</th><th>System</th><th>Data object</th><th class="num">Tokens</th><th class="num">Conf.</th><th>Rationale</th></tr></thead>
      <tbody id="labels-tbody"></tbody>
    </table>
  </section>
  <section id="cost">
    <table>
      <thead><tr><th>CAGE</th><th class="num">Action count</th><th class="num">Total tokens (est.)</th><th class="num">Avg / action</th></tr></thead>
      <tbody id="cost-tbody"></tbody>
    </table>
    <p class="empty" id="cost-empty">Cost rows fill in once labels are present.</p>
  </section>
</main>
<script id="payload" type="application/json">__PAYLOAD_JSON__</script>
<script>
(function () {
  const data = JSON.parse(document.getElementById('payload').textContent);
  document.getElementById('meta-line').textContent =
    `Generated ${data.generated_at} — ${data.events.length} events, ${data.sessions.length} sessions, ${data.labels.length} labels`;

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

  // Tabs
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('section').forEach(s => s.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(btn.dataset.tab).classList.add('active');
    });
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

  // Labels
  const ltb = document.getElementById('labels-tbody');
  data.labels.forEach(l => {
    ltb.innerHTML += `
      <tr>
        <td>${escapeHtml(l.action_id)}</td>
        <td><span class="pill pill-${l.cage_label}">${l.cage_label}</span></td>
        <td>${escapeHtml(l.system)}</td>
        <td>${escapeHtml(l.data_object)}</td>
        <td class="num">${l.estimated_tokens.toLocaleString()}</td>
        <td class="num">${l.confidence.toFixed(2)}</td>
        <td>${escapeHtml(l.rationale)}</td>
      </tr>`;
  });
  if (data.labels.length === 0) ltb.innerHTML = '<tr><td colspan="7" class="empty">No labels yet.</td></tr>';

  // Cost
  const ctb = document.getElementById('cost-tbody');
  const labels = ['C', 'A', 'G', 'E'];
  const names = { C: 'Capture', A: 'Analyze', G: 'Generate', E: 'Extract' };
  let any = false;
  labels.forEach(k => {
    const r = data.cost_summary[k];
    if (!r) return;
    any = true;
    const avg = r.count ? Math.round(r.tokens / r.count) : 0;
    ctb.innerHTML += `
      <tr>
        <td><span class="pill pill-${k}">${k}</span> ${names[k]}</td>
        <td class="num">${r.count}</td>
        <td class="num">${r.tokens.toLocaleString()}</td>
        <td class="num">${avg.toLocaleString()}</td>
      </tr>`;
  });
  if (any) document.getElementById('cost-empty').style.display = 'none';

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[c]);
  }
})();
</script>
</body>
</html>
"""
