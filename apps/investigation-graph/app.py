"""
Host Investigation Graph — Interactive Security Triage Visualization

A Streamlit Databricks App that builds activity timelines for a host
and renders interactive graphs showing user actions, process executions,
authentication events, and system activity.

Security note: The vis.js graph HTML is constructed from internal Delta
table data only (not user-supplied HTML). Node labels and tooltips use
the vis.js DataSet API for safe rendering. Detail panel uses DOM
textContent methods.
"""

import os
import json
from datetime import datetime

import streamlit as st
from databricks.sdk.core import Config

from backend import get_connection, list_hosts, build_host_timeline, derive_graph, build_ip_hostname_map, build_command_index, resolve_host_ips

# ---------------------------------------------------------------------------
# Page config — must be first Streamlit command
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Host Investigation",
    page_icon="🔍",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Connection (cached)
# ---------------------------------------------------------------------------
@st.cache_resource(ttl=300)
def _get_conn():
    cfg = Config()
    warehouse_id = os.getenv("DATABRICKS_WAREHOUSE_ID", "")
    return get_connection(cfg, warehouse_id)

@st.cache_data(ttl=120)
def _list_hosts():
    return list_hosts(_get_conn())

@st.cache_data(ttl=60, show_spinner="Querying host timeline...")
def _query_timeline(hostname, start_str, end_str):
    """Cached timeline query — only re-runs when params change."""
    conn = _get_conn()
    return build_host_timeline(conn, hostname, start_str, end_str)

# ---------------------------------------------------------------------------
# Sidebar — inputs
# ---------------------------------------------------------------------------
st.sidebar.title("Investigation")

available_hosts = _list_hosts()

with st.sidebar.form("params"):
    hostname = st.selectbox("Host", available_hosts, index=0 if available_hosts else None,
                            help="Select a host to investigate")

    st.markdown("**Time Range**")
    col_start, col_end = st.columns(2)
    with col_start:
        start_dt = st.date_input("Start date", value=None)
        start_tm_str = st.text_input("Start time", placeholder="HH:MM")
    with col_end:
        end_dt = st.date_input("End date", value=None)
        end_tm_str = st.text_input("End time", placeholder="HH:MM")

    submitted = st.form_submit_button("Investigate", type="primary")

# Store investigation params in session_state so filters don't reset
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
    st.session_state["inv_hostname"] = hostname
    st.session_state["inv_start"] = str(start_combined)
    st.session_state["inv_end"] = str(end_combined)


# ---------------------------------------------------------------------------
# Main area
# ---------------------------------------------------------------------------
st.title("Host Investigation Graph")

if "inv_hostname" not in st.session_state:
    st.info("Select a host and click **Investigate** to begin.")
    st.stop()

# ---------------------------------------------------------------------------
# Build timeline (cached — doesn't re-query on filter changes)
# ---------------------------------------------------------------------------
inv_host = st.session_state["inv_hostname"]
inv_start = st.session_state["inv_start"]
inv_end = st.session_state["inv_end"]

timeline = _query_timeline(inv_host, inv_start, inv_end)

if not timeline:
    st.warning(f"No activity found for **{inv_host}** in the selected time window.")
    st.stop()

# ---------------------------------------------------------------------------
# Stats (from full unfiltered timeline)
# ---------------------------------------------------------------------------
categories = {}
users_seen = set()
for row in timeline:
    cat = row.get("event_category", "unknown")
    categories[cat] = categories.get(cat, 0) + 1
    if row.get("user"):
        users_seen.add(row["user"])

col1, col2, col3, col4, col5, col6 = st.columns(6)
col1.metric("Total Events", len(timeline))
col2.metric("Users", len(users_seen))
col3.metric("Auth", categories.get("authentication", 0))
col4.metric("Commands", categories.get("process_execution", 0))
col5.metric("Network", categories.get("network_connection", 0))
col6.metric("System", categories.get("system_event", 0))

st.caption(f"{len(timeline)} events for `{inv_host}`")

# ---------------------------------------------------------------------------
# Derive graph from filtered timeline
# ---------------------------------------------------------------------------
ip_map = build_ip_hostname_map(_get_conn())
nodes, edges = derive_graph(timeline, ip_hostname_map=ip_map)
host_ips = resolve_host_ips(_get_conn(), inv_host)
commands_index = build_command_index(timeline, conn=_get_conn(), host_ips=host_ips, end_time=inv_end)

nodes_json = json.dumps(nodes)
edges_json = json.dumps(edges)
commands_json = json.dumps(commands_index)

# ---------------------------------------------------------------------------
# Render vis.js graph
# ---------------------------------------------------------------------------
graph_template = """
<!DOCTYPE html>
<html>
<head>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  body { margin: 0; background: #1a1a2e; font-family: -apple-system, BlinkMacSystemFont, sans-serif; color: #f8f8f2; }
  #controls {
    display: flex; flex-direction: row; padding: 8px 12px;
    background: #282a36; border-bottom: 1px solid #44475a;
    flex-wrap: wrap; font-size: 12px; align-items: center; gap: 6px;
  }
  #controls label { cursor: pointer; display: flex; align-items: center; gap: 4px; }
  #controls input[type=checkbox] { accent-color: #bd93f9; }
  .user-toggle {
    padding: 2px 8px; border-radius: 10px; border: 1px solid #44475a;
    cursor: pointer; font-size: 11px; background: #1a1a2e; color: #f8f8f2;
  }
  .user-toggle.active { background: #bd93f9; color: #1a1a2e; border-color: #bd93f9; }
  #graph-container { position: relative; }
  #graph { width: 100%; height: 560px; }
  #detail {
    display: none; position: absolute; right: 16px; top: 16px; width: 300px;
    background: #282a36; border: 1px solid #44475a; border-radius: 8px;
    padding: 16px; font-size: 13px; max-height: 400px; overflow-y: auto;
    z-index: 10; font-family: monospace;
  }
  .cmd-panel {
    position: absolute; background: #282a36; border: 1px solid #bd93f9;
    border-radius: 6px; font-size: 11px; font-family: monospace;
    z-index: 5; min-width: 250px; max-width: 400px; display: none;
  }
  .cmd-panel-header {
    padding: 6px 10px; background: #1e1e30; border-radius: 6px 6px 0 0;
    cursor: pointer; display: flex; justify-content: space-between; color: #8be9fd;
  }
  .cmd-panel-body { max-height: 200px; overflow-y: auto; padding: 4px 10px; }
  .cmd-row {
    display: flex; gap: 8px; padding: 2px 0; border-bottom: 1px solid #1a1a2e;
  }
  .cmd-time { color: #6272a4; flex: 0 0 65px; font-size: 10px; }
  .cmd-text { color: #f1fa8c; word-break: break-all; }
  .cmd-ssh { color: #ff79c6; }
</style>
</head>
<body>
<div id="controls">
  <span style="color:#6272a4;margin-right:4px">Filters:</span>
  <label>
    <input type="checkbox" checked onchange="filterByCategory('authentication', this.checked)"> Auth
  </label>
  <label>
    <input type="checkbox" checked onchange="filterByCategory('process_execution', this.checked)"> Commands
  </label>
  <label>
    <input type="checkbox" checked onchange="filterByCategory('account_change', this.checked)"> Acct Changes
  </label>
  <label>
    <input type="checkbox" checked onchange="filterByCategory('network_connection', this.checked)"> Network
  </label>
  <label>
    <input type="checkbox" onchange="filterByCategory('system_event', this.checked)"> System
  </label>
  <span style="color:#44475a;margin:0 4px">|</span>
  <span style="color:#6272a4">Users:</span>
  <span id="user-toggles"></span>
  <span style="color:#44475a;margin:0 4px">|</span>
  <button onclick="network.fit({animation:true})" style="background:#44475a;color:#f8f8f2;border:none;border-radius:4px;padding:2px 10px;cursor:pointer;font-size:11px">Reset View</button>
</div>
<div id="graph-container">
  <div id="graph"></div>
  <div id="detail"></div>
</div>
<script>
var allNodes = NODES_JSON;
var allEdges = EDGES_JSON;
var commandsIndex = COMMANDS_JSON;

var openPanels = {};   // nodeId -> DOM element
var categoryState = {
  authentication: true,
  process_execution: true,
  account_change: true,
  network_connection: true,
  system_event: false
};
var userState = {};    // nodeId -> bool (true = visible)

var nodes = new vis.DataSet(allNodes);
var edges = new vis.DataSet(allEdges);
var network = new vis.Network(document.getElementById('graph'), {nodes: nodes, edges: edges}, {
  layout: {
    hierarchical: {
      direction: 'LR',
      sortMethod: 'directed',
      levelSeparation: 200,
      nodeSpacing: 80
    }
  },
  physics: { enabled: true, hierarchicalRepulsion: { nodeDistance: 120 } },
  interaction: { hover: true, tooltipDelay: 200 },
  edges: { arrows: { to: { enabled: true, scaleFactor: 0.5 } }, smooth: { type: 'cubicBezier' } }
});
network.once('stabilized', function() {
  // 1. Capture positions computed by hierarchical layout
  var positions = network.getPositions();
  // 2. Turn off hierarchical layout AND physics
  network.setOptions({ layout: { hierarchical: { enabled: false } }, physics: false });
  // 3. Re-apply positions and pin every node as fixed
  var updates = [];
  Object.keys(positions).forEach(function(id) {
    updates.push({ id: id, x: positions[id].x, y: positions[id].y, fixed: true });
  });
  nodes.update(updates);
});

// Unfix a node when the user starts dragging it, re-fix on release
network.on('dragStart', function(p) {
  if (p.nodes.length > 0) {
    nodes.update({ id: p.nodes[0], fixed: false });
  }
});
network.on('dragEnd', function(p) {
  if (p.nodes.length > 0) {
    var pos = network.getPositions([p.nodes[0]]);
    var nodePos = pos[p.nodes[0]];
    nodes.update({ id: p.nodes[0], x: nodePos.x, y: nodePos.y, fixed: true });
  }
});

// Build user toggle buttons for each user node
allNodes.forEach(function(node) {
  if (node.type === 'user') {
    userState[node.id] = true;
    var btn = document.createElement('span');
    btn.className = 'user-toggle active';
    btn.setAttribute('data-uid', node.id);
    // Use only the first line of the label as the button text
    var labelText = (node.label || String(node.id)).split('\\n')[0];
    btn.textContent = labelText;
    btn.onclick = (function(uid) {
      return function() {
        var nowVisible = !userState[uid];
        userState[uid] = nowVisible;
        btn.className = 'user-toggle' + (nowVisible ? ' active' : '');
        filterByUser(uid, nowVisible);
      };
    })(node.id);
    document.getElementById('user-toggles').appendChild(btn);
  }
});

function filterByCategory(cat, visible) {
  categoryState[cat] = visible;
  nodes.get().forEach(function(node) {
    if (node.event_category === cat) {
      nodes.update({ id: node.id, hidden: !visible });
    }
  });
  edges.get().forEach(function(edge) {
    if (edge.event_category === cat) {
      edges.update({ id: edge.id, hidden: !visible });
    }
  });
  // Show/hide command panels for process_execution nodes
  if (cat === 'process_execution') {
    Object.keys(openPanels).forEach(function(nodeId) {
      var panel = openPanels[nodeId];
      if (panel) {
        panel.style.display = visible ? 'block' : 'none';
      }
    });
  }
}

function filterByUser(uid, visible) {
  nodes.update({ id: uid, hidden: !visible });
  edges.get().forEach(function(edge) {
    if (edge.from === uid || edge.to === uid) {
      if (edge.event_category === 'structural') {
        edges.update({ id: edge.id, hidden: !visible });
      } else {
        // Only show if both user visible AND category filter allows it
        var catAllowed = categoryState[edge.event_category] !== false;
        edges.update({ id: edge.id, hidden: !(visible && catAllowed) });
      }
    }
  });
  if (openPanels[uid]) {
    openPanels[uid].style.display = visible ? 'block' : 'none';
  }
}

function positionPanel(nodeId) {
  var panel = openPanels[nodeId];
  if (!panel) { return; }
  try {
    var pos = network.getPosition(nodeId);
    var dom = network.canvasToDOM(pos);
    panel.style.left = (dom.x + 20) + 'px';
    panel.style.top  = (dom.y + 20) + 'px';
  } catch(e) {}
}

function repositionAllPanels() {
  Object.keys(openPanels).forEach(function(nodeId) {
    positionPanel(nodeId);
  });
}

function toggleCmdPanel(nodeId) {
  // If panel already exists, toggle visibility
  if (openPanels[nodeId]) {
    var existing = openPanels[nodeId];
    existing.style.display = (existing.style.display === 'none') ? 'block' : 'none';
    return;
  }
  var data = commandsIndex[nodeId];
  if (!data || !data.commands || data.commands.length === 0) { return; }

  var panel = document.createElement('div');
  panel.className = 'cmd-panel';

  // Header
  var header = document.createElement('div');
  header.className = 'cmd-panel-header';

  var leftSpan = document.createElement('span');
  leftSpan.textContent = '\u25bc ' + data.label + ' (' + data.commands.length + ' cmds)';

  var rightSpan = document.createElement('span');
  rightSpan.style.color = '#6272a4';
  rightSpan.textContent = 'click to close';

  header.appendChild(leftSpan);
  header.appendChild(rightSpan);
  header.onclick = function() { panel.style.display = 'none'; };

  // Body
  var body = document.createElement('div');
  body.className = 'cmd-panel-body';

  data.commands.forEach(function(cmd) {
    var row = document.createElement('div');
    row.className = 'cmd-row';

    var timeSpan = document.createElement('span');
    timeSpan.className = 'cmd-time';
    // Extract HH:MM:SS portion from ISO timestamp string
    var timeStr = (cmd.time || '');
    timeSpan.textContent = timeStr.length >= 19 ? timeStr.substring(11, 19) : timeStr;

    var cmdSpan = document.createElement('span');
    cmdSpan.className = 'cmd-text' + (cmd.is_ssh ? ' cmd-ssh' : '');
    cmdSpan.textContent = cmd.command_line || '';

    row.appendChild(timeSpan);
    row.appendChild(cmdSpan);
    body.appendChild(row);
  });

  panel.appendChild(header);
  panel.appendChild(body);
  document.getElementById('graph-container').appendChild(panel);

  openPanels[nodeId] = panel;
  positionPanel(nodeId);
  panel.style.display = 'block';
}

// Reposition panels on zoom/drag/animation events
network.on('zoom', repositionAllPanels);
network.on('dragEnd', repositionAllPanels);
network.on('animationFinished', repositionAllPanels);

network.on('click', function(p) {
  var panel = document.getElementById('detail');
  if (p.nodes.length > 0) {
    var nodeId = p.nodes[0];
    var node = allNodes.find(function(n) { return n.id === nodeId; });
    if (node && (node.type === 'user' || node.type === 'ssh_target')) {
      // User node: toggle command panel, hide detail
      toggleCmdPanel(nodeId);
      panel.style.display = 'none';
      return;
    }
    if (node) {
      panel.textContent = '';
      var h3 = document.createElement('h3');
      h3.style.marginTop = '0';
      h3.style.color = node.color || '#f8f8f2';
      h3.textContent = node.label || node.id;
      panel.appendChild(h3);
      var typeDiv = document.createElement('div');
      typeDiv.textContent = 'Type: ' + (node.type || 'unknown');
      panel.appendChild(typeDiv);
      if (node.title) {
        var titleDiv = document.createElement('div');
        titleDiv.style.marginTop = '8px';
        titleDiv.style.color = '#6272a4';
        titleDiv.style.whiteSpace = 'pre-wrap';
        titleDiv.textContent = node.title;
        panel.appendChild(titleDiv);
      }
      panel.style.display = 'block';
    }
  } else if (p.edges.length > 0) {
    var edge = allEdges.find(function(e) { return e.id === p.edges[0]; });
    if (edge && edge.title) {
      panel.textContent = '';
      var h3 = document.createElement('h3');
      h3.style.marginTop = '0';
      h3.style.color = '#8be9fd';
      h3.textContent = edge.label || 'Edge';
      panel.appendChild(h3);
      var detDiv = document.createElement('div');
      detDiv.style.color = '#6272a4';
      detDiv.style.whiteSpace = 'pre-wrap';
      detDiv.textContent = edge.title;
      panel.appendChild(detDiv);
      panel.style.display = 'block';
    }
  } else {
    panel.style.display = 'none';
  }
});

// Hide system events by default on init
filterByCategory('system_event', false);
</script>
</body>
</html>
"""

graph_html = (graph_template
    .replace("NODES_JSON", nodes_json)
    .replace("EDGES_JSON", edges_json)
    .replace("COMMANDS_JSON", commands_json))

html_size = len(graph_html)
if html_size > 10_000_000:
    st.warning(f"Graph HTML is {html_size / 1_000_000:.1f}MB. Consider narrowing the time window or filters.")

st.components.v1.html(graph_html, height=650, scrolling=False)

# ---------------------------------------------------------------------------
# Events table with same filters applied
# ---------------------------------------------------------------------------
with st.expander("All Events", expanded=False):
    display_cols = ["time", "event_category", "action", "user", "detail",
                    "source_ip", "source_host"]
    display_data = [{k: row.get(k) for k in display_cols} for row in timeline]
    st.caption(f"{len(display_data)} events")
    st.dataframe(display_data, use_container_width=True, height=400)
