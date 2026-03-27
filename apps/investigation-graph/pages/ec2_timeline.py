"""
EC2 Config Timeline — CloudTrail + AWS Config CDC timeline for EC2 instances

A Streamlit page that renders an interactive timeline of API calls and
configuration changes for a selected EC2 instance.

Security note: All dynamic content in the timeline HTML uses createElement +
textContent — never innerHTML with data.  timeline_json is injected into a
var assignment only; the JS renderer uses DOM APIs throughout.
"""

import os
import json
from datetime import datetime

import streamlit as st
from databricks.sdk.core import Config

from backend import get_connection
from ec2_timeline_backend import (
    list_instances,
    get_instance_details,
    build_ec2_timeline,
    collapse_service_polls,
)

# ---------------------------------------------------------------------------
# Page config — must be first Streamlit command
# ---------------------------------------------------------------------------
st.set_page_config(page_title="EC2 Timeline", page_icon="📋", layout="wide")

# ---------------------------------------------------------------------------
# Connection (cached — same pattern as app.py)
# ---------------------------------------------------------------------------
@st.cache_resource(ttl=300)
def _get_conn():
    cfg = Config()
    warehouse_id = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
    return get_connection(cfg, warehouse_id)


# ---------------------------------------------------------------------------
# Sidebar — instance selector and time range
# ---------------------------------------------------------------------------
st.sidebar.title("EC2 Timeline")

instances = list_instances(_get_conn())
instance_options = [f"{i.get('name', '')} ({i['instance_id']})" for i in instances]
instance_ids = [i["instance_id"] for i in instances]

with st.sidebar.form("params"):
    selected_idx = st.selectbox(
        "Instance",
        range(len(instance_options)),
        format_func=lambda i: instance_options[i],
        index=0 if instances else None,
        help="Select an EC2 instance to investigate",
    )

    st.markdown("**Time Range**")
    col_start, col_end = st.columns(2)
    with col_start:
        start_dt = st.date_input("Start date", value=None)
        start_tm_str = st.text_input("Start time", placeholder="HH:MM")
    with col_end:
        end_dt = st.date_input("End date", value=None)
        end_tm_str = st.text_input("End time", placeholder="HH:MM")

    submitted = st.form_submit_button("View Timeline", type="primary")

# Store params in session_state so filter changes don't reset the investigation
if submitted:
    if not start_dt or not end_dt or not start_tm_str.strip() or not end_tm_str.strip():
        st.sidebar.error("Please fill in all date and time fields.")
        st.stop()
    try:
        start_tm = datetime.strptime(start_tm_str.strip(), "%H:%M").time()
        end_tm = datetime.strptime(end_tm_str.strip(), "%H:%M").time()
    except ValueError:
        st.sidebar.error("Times must be in HH:MM format (e.g. 14:30).")
        st.stop()
    start_combined = datetime.combine(start_dt, start_tm)
    end_combined = datetime.combine(end_dt, end_tm)
    if start_combined >= end_combined:
        st.sidebar.error("Start must be before end.")
        st.stop()
    st.session_state["ec2_instance_id"] = instance_ids[selected_idx]
    st.session_state["ec2_start"] = str(start_combined)
    st.session_state["ec2_end"] = str(end_combined)


# ---------------------------------------------------------------------------
# Main area
# ---------------------------------------------------------------------------
st.title("EC2 Config Timeline")

if "ec2_instance_id" not in st.session_state:
    st.info("Select an EC2 instance and click **View Timeline** to begin.")
    st.stop()

instance_id = st.session_state["ec2_instance_id"]
inv_start = st.session_state["ec2_start"]
inv_end = st.session_state["ec2_end"]

# ---------------------------------------------------------------------------
# Instance header card
# ---------------------------------------------------------------------------
details = get_instance_details(_get_conn(), instance_id)
if details:
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Instance", details.get("name") or details["instance_id"])
    col2.metric("State", details.get("instance_state", "unknown"))
    col3.metric("Type", details.get("instance_type", ""))
    col4.metric("Private IP", details.get("private_ip", ""))
    col5.metric("Account", details.get("aws_account_id", ""))

    with st.expander("Tags & Security Groups"):
        if details.get("tags"):
            st.json(details["tags"])
        if details.get("security_groups"):
            st.write("Security Groups:", ", ".join(details["security_groups"]))

# ---------------------------------------------------------------------------
# Build and post-process timeline
# ---------------------------------------------------------------------------
timeline = build_ec2_timeline(_get_conn(), instance_id, inv_start, inv_end)
timeline = collapse_service_polls(timeline)

if not timeline:
    st.warning(f"No activity found for **{instance_id}** in the selected time window.")
    st.stop()

st.caption(f"{len(timeline)} events")

# Strip heavy fields before sending to the JS template — user_agent and detail
# are only needed on expand; keep them short to avoid bloating the HTML.
for evt in timeline:
    ua = evt.get("user_agent", "")
    if len(ua) > 120:
        evt["user_agent"] = ua[:120] + "..."
    d = evt.get("detail", "")
    if len(d) > 300:
        evt["detail"] = d[:300] + "..."

timeline_json = json.dumps(timeline, default=str)
# Escape </ to prevent </script> from breaking the JS context (same risk as app.py)
timeline_json = timeline_json.replace("</", r"<\/")

# ---------------------------------------------------------------------------
# Timeline HTML/CSS/JS template (Dracula dark theme)
# All dynamic content uses createElement + textContent — no innerHTML with data.
# TIMELINE_JSON is injected as a JS var assignment; the renderer never
# writes that data into the DOM via innerHTML.
# ---------------------------------------------------------------------------
timeline_template = """
<!DOCTYPE html>
<html>
<head>
<style>
body { margin:0; background:#1a1a2e; font-family:-apple-system,BlinkMacSystemFont,sans-serif; color:#f8f8f2; }
#controls { padding:8px 12px; background:#282a36; border-bottom:1px solid #44475a; display:flex; gap:16px; align-items:center; flex-wrap:wrap; font-size:12px; }
#controls label { cursor:pointer; display:flex; align-items:center; gap:4px; }
#controls input[type=checkbox] { accent-color:#bd93f9; }
#controls button { background:#44475a; color:#f8f8f2; border:none; border-radius:4px; padding:2px 10px; cursor:pointer; font-size:11px; }
.timeline-container { position:relative; padding:20px 20px 20px 40px; max-width:900px; margin:0 auto; }
.timeline-line { position:absolute; left:20px; top:0; bottom:0; width:2px; background:#44475a; }
.timeline-entry { position:relative; margin-bottom:12px; }
.timeline-dot { position:absolute; left:-28px; top:12px; width:12px; height:12px; border-radius:50%; border:2px solid #44475a; background:#1a1a2e; }
.entry-card { background:#282a36; border-left:3px solid #44475a; border-radius:0 6px 6px 0; padding:10px 14px; cursor:pointer; transition:background 0.2s; }
.entry-card:hover { background:#2d2f3d; }
.entry-card.failure { background:#2a1a1e; }
.entry-header { display:flex; justify-content:space-between; align-items:center; }
.entry-time { color:#6272a4; font-size:11px; font-family:monospace; }
.entry-op { font-weight:bold; font-size:13px; margin:0 8px; flex:1; }
.source-badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:10px; font-weight:bold; }
.entry-who { color:#6272a4; font-size:11px; margin-top:2px; }
.entry-detail { display:none; margin-top:8px; padding-top:8px; border-top:1px solid #44475a; font-size:11px; color:#6272a4; font-family:monospace; white-space:pre-wrap; max-height:200px; overflow-y:auto; }
.collapsed-group { background:#1e1e30; border-left:3px solid #6272a4; border-radius:0 6px 6px 0; padding:8px 14px; cursor:pointer; font-size:12px; color:#6272a4; margin-bottom:12px; position:relative; }
.collapsed-group-events { display:none; margin-top:8px; padding-top:8px; border-top:1px solid #44475a; }
.config-pill { display:inline-block; padding:1px 8px; border-radius:10px; font-size:10px; font-weight:bold; }
.scroll-nav { position:fixed; right:20px; bottom:20px; display:flex; flex-direction:column; gap:4px; }
.scroll-nav button { background:#44475a; color:#f8f8f2; border:none; border-radius:4px; padding:4px 8px; cursor:pointer; font-size:12px; }
</style>
</head>
<body>
<div id="controls">
  <span style="color:#6272a4">Sources:</span>
  <label><input type="checkbox" checked onchange="toggleSource('iac',this.checked)"> IaC</label>
  <label><input type="checkbox" checked onchange="toggleSource('console',this.checked)"> Console</label>
  <label><input type="checkbox" checked onchange="toggleSource('cli',this.checked)"> CLI</label>
  <label><input type="checkbox" checked onchange="toggleSource('sdk',this.checked)"> Script</label>
  <label><input type="checkbox" onchange="toggleSource('service',this.checked)"> Service</label>
  <label><input type="checkbox" checked onchange="toggleSource('unknown',this.checked)"> Other</label>
  <span style="color:#44475a;margin:0 4px">|</span>
  <label><input type="checkbox" onchange="toggleReadOnly(this.checked)"> Show read-only ops</label>
  <span style="color:#44475a;margin:0 4px">|</span>
  <button onclick="toggleSort()">&#8645; Reverse order</button>
</div>
<div class="timeline-container">
  <div class="timeline-line"></div>
  <div id="timeline-entries"></div>
</div>
<div class="scroll-nav">
  <button onclick="window.scrollTo({top:0,behavior:'smooth'})">&#8593;</button>
  <button onclick="window.scrollTo({top:document.body.scrollHeight,behavior:'smooth'})">&#8595;</button>
</div>
<script>
var timelineData = TIMELINE_JSON;
var sourceFilters = {iac:true, console:true, cli:true, sdk:true, service:false, unknown:true, config:true};
var showReadOnly = false;
var sortNewest = true;

function toggleSource(s, v) { sourceFilters[s] = v; renderTimeline(); }
function toggleReadOnly(v) { showReadOnly = v; renderTimeline(); }
function toggleSort() { sortNewest = !sortNewest; renderTimeline(); }

function renderTimeline() {
  var container = document.getElementById('timeline-entries');
  container.textContent = '';  // clear safely — no innerHTML

  var sorted = timelineData.slice();
  if (sortNewest) sorted.reverse();

  sorted.forEach(function(evt) {
    var srcType = evt.source ? evt.source.source_type : 'unknown';
    if (!sourceFilters[srcType]) return;
    if (evt.is_read_only && !showReadOnly) return;

    if (evt.event_type === 'service_poll_group') {
      renderGroupEntry(container, evt);
    } else {
      renderEventEntry(container, evt);
    }
  });

  if (container.children.length === 0) {
    var empty = document.createElement('div');
    empty.style.cssText = 'text-align:center;color:#6272a4;padding:40px;';
    empty.textContent = 'No events match the current filters.';
    container.appendChild(empty);
  }
}

function renderEventEntry(container, evt) {
  var entry = document.createElement('div');
  entry.className = 'timeline-entry';

  // Dot — colored by source type
  var dot = document.createElement('div');
  dot.className = 'timeline-dot';
  var srcColor = evt.source ? evt.source.color : '#44475a';
  dot.style.borderColor = srcColor;
  entry.appendChild(dot);

  // Card
  var card = document.createElement('div');
  card.className = 'entry-card' + (evt.is_failure ? ' failure' : '');
  card.style.borderLeftColor = evt.is_failure ? '#ff5555' : srcColor;

  // Header row
  var header = document.createElement('div');
  header.className = 'entry-header';

  var timeEl = document.createElement('span');
  timeEl.className = 'entry-time';
  timeEl.textContent = evt.time ? evt.time.substring(0, 19).replace('T', ' ') : '';
  header.appendChild(timeEl);

  var opEl = document.createElement('span');
  opEl.className = 'entry-op';
  opEl.textContent = evt.operation || '';
  header.appendChild(opEl);

  // Source badge — background from classify_source color palette
  var badge = document.createElement('span');
  badge.className = 'source-badge';
  badge.style.background = srcColor;
  // Use dark text on light badge colors for readability
  badge.style.color = (srcColor === '#f8f8f2' || srcColor === '#f1fa8c' || srcColor === '#50fa7b' || srcColor === '#ffb86c') ? '#1a1a2e' : '#f8f8f2';
  badge.textContent = evt.source ? evt.source.label : 'Unknown';
  header.appendChild(badge);

  // Success / failure indicator
  var statusEl = document.createElement('span');
  statusEl.style.marginLeft = '6px';
  statusEl.textContent = evt.is_failure ? '\u2715' : '\u2713';
  statusEl.style.color = evt.is_failure ? '#ff5555' : '#50fa7b';
  header.appendChild(statusEl);

  card.appendChild(header);

  // "Who" line — principal + source IP
  var whoEl = document.createElement('div');
  whoEl.className = 'entry-who';
  var whoParts = [];
  if (evt.who) whoParts.push(evt.who);
  if (evt.source_ip) whoParts.push('from ' + evt.source_ip);
  whoEl.textContent = whoParts.join(' ');
  card.appendChild(whoEl);

  // Config change type pill (INSERT / UPDATE / DELETE)
  if (evt.event_type === 'config' && evt.change_type) {
    var pill = document.createElement('span');
    pill.className = 'config-pill';
    pill.style.marginTop = '4px';
    pill.style.display = 'inline-block';
    var pillColors = {INSERT:'#50fa7b', UPDATE:'#f1fa8c', DELETE:'#ff5555'};
    pill.style.background = pillColors[evt.change_type] || '#6272a4';
    pill.style.color = '#1a1a2e';
    pill.textContent = evt.change_type;
    card.appendChild(pill);
  }

  // Detail pane (hidden by default — click card to expand)
  var detail = document.createElement('div');
  detail.className = 'entry-detail';
  var detailParts = [];
  if (evt.user_agent) detailParts.push('User Agent: ' + evt.user_agent);
  if (evt.status_detail) detailParts.push('Status: ' + evt.status_detail);
  if (evt.detail) detailParts.push(evt.detail);
  detail.textContent = detailParts.join('\\n\\n');
  card.appendChild(detail);

  // Click to toggle detail pane
  card.onclick = function() {
    detail.style.display = (detail.style.display === 'none' || !detail.style.display) ? 'block' : 'none';
  };

  entry.appendChild(card);
  container.appendChild(entry);
}

function renderGroupEntry(container, evt) {
  var group = document.createElement('div');
  group.className = 'collapsed-group';

  var summary = document.createElement('span');
  summary.textContent = evt.operation
    + ' ('
    + (evt.time ? evt.time.substring(11, 19) : '')
    + ' \u2014 '
    + (evt.time_end ? evt.time_end.substring(11, 19) : '')
    + ')';
  group.appendChild(summary);

  // Expandable list of the underlying service poll events
  var eventsDiv = document.createElement('div');
  eventsDiv.className = 'collapsed-group-events';
  if (evt.events) {
    evt.events.forEach(function(subEvt) {
      var line = document.createElement('div');
      line.style.cssText = 'padding:2px 0;font-size:11px;border-bottom:1px solid #1a1a2e;';
      line.textContent = (subEvt.time ? subEvt.time.substring(11, 19) : '')
        + '  '
        + (subEvt.operation || '')
        + '  '
        + (subEvt.who || '');
      eventsDiv.appendChild(line);
    });
  }
  group.appendChild(eventsDiv);

  group.onclick = function() {
    eventsDiv.style.display = (eventsDiv.style.display === 'none' || !eventsDiv.style.display) ? 'block' : 'none';
  };

  container.appendChild(group);
}

// Initial render — wait for DOM to be ready
document.addEventListener('DOMContentLoaded', function() {
  try {
    renderTimeline();
  } catch(e) {
    var errDiv = document.getElementById('timeline-entries');
    if (errDiv) {
      errDiv.textContent = 'Render error: ' + e.message;
      errDiv.style.color = '#ff5555';
      errDiv.style.padding = '20px';
    }
  }
});
// Fallback: if DOMContentLoaded already fired, run immediately
if (document.readyState !== 'loading') {
  try { renderTimeline(); } catch(e) { /* logged above */ }
}
</script>
</body>
</html>
"""

timeline_html = timeline_template.replace("TIMELINE_JSON", timeline_json)
html_size = len(timeline_html)
st.caption(f"HTML size: {html_size / 1_000:.0f}KB")
if html_size > 5_000_000:
    st.error(
        f"Timeline HTML is {html_size / 1_000_000:.1f}MB — too large to render. "
        "Narrow the time window or use the workload-a instance with recent mutations."
    )
    st.stop()
st.components.v1.html(timeline_html, height=800, scrolling=True)

# ---------------------------------------------------------------------------
# Raw events table (collapsed by default)
# ---------------------------------------------------------------------------
with st.expander("All Events", expanded=False):
    display_cols = ["time", "event_type", "operation", "who", "source_ip", "status"]
    display_data = [
        {k: evt.get(k) for k in display_cols}
        for evt in timeline
        if evt.get("event_type") != "service_poll_group"
    ]
    st.dataframe(display_data, use_container_width=True, height=400)
