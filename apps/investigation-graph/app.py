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

from backend import get_connection, list_hosts, build_host_timeline, derive_graph, build_ip_hostname_map

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
# Sidebar — filters (outside form so they update without re-querying)
# ---------------------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.subheader("Filters")
show_auth = st.sidebar.checkbox("Authentication", value=True)
show_commands = st.sidebar.checkbox("Commands", value=True)
show_accounts = st.sidebar.checkbox("Account Changes", value=True)
show_network = st.sidebar.checkbox("Network (mgmt ports)", value=True)
show_system = st.sidebar.checkbox("System Events", value=False)

active_filters = {
    "authentication": show_auth,
    "process_execution": show_commands,
    "account_change": show_accounts,
    "network_connection": show_network,
    "system_event": show_system,
}

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

# ---------------------------------------------------------------------------
# Sidebar — user filter (populated from timeline data)
# ---------------------------------------------------------------------------
all_users = sorted(users_seen)
selected_users = st.sidebar.multiselect("Users", all_users, default=all_users)

# Apply all filters to timeline
filtered_timeline = [
    r for r in timeline
    if active_filters.get(r.get("event_category"), True)
    and (r.get("user", "") in selected_users or not r.get("user"))
]

st.caption(f"Showing {len(filtered_timeline)} of {len(timeline)} events for `{inv_host}`")

# ---------------------------------------------------------------------------
# Derive graph from filtered timeline
# ---------------------------------------------------------------------------
ip_map = build_ip_hostname_map(_get_conn())
nodes, edges = derive_graph(filtered_timeline, ip_hostname_map=ip_map)

nodes_json = json.dumps(nodes)
edges_json = json.dumps(edges)

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
  #graph { width: 100%; height: 600px; border: 1px solid #44475a; border-radius: 8px; }
  #detail { display: none; position: absolute; right: 16px; top: 16px; width: 300px; background: #282a36; border: 1px solid #44475a; border-radius: 8px; padding: 16px; font-size: 13px; max-height: 400px; overflow-y: auto; z-index: 10; font-family: monospace; }
</style>
</head>
<body>
<div style="position:relative">
  <div id="graph"></div>
  <div id="detail"></div>
</div>
<script>
var allNodes = NODES_JSON;
var allEdges = EDGES_JSON;

var nodes = new vis.DataSet(allNodes);
var edges = new vis.DataSet(allEdges);
var network = new vis.Network(document.getElementById('graph'), {nodes:nodes, edges:edges}, {
  layout: { hierarchical: { direction:'LR', sortMethod:'directed', levelSeparation: 180, nodeSpacing: 60 } },
  physics: { enabled:true, hierarchicalRepulsion:{ nodeDistance: 100 } },
  interaction: { hover:true, tooltipDelay:200 },
  edges: { arrows:{ to:{ enabled:true, scaleFactor:0.5 } }, smooth:{ type:'cubicBezier' } }
});
network.once('stabilized', function() { network.setOptions({physics:false}); });

network.on('click', function(p) {
  var panel = document.getElementById('detail');
  if (p.nodes.length > 0) {
    var node = allNodes.find(function(n){ return n.id === p.nodes[0]; });
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
    var edge = allEdges.find(function(e){ return e.id === p.edges[0]; });
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
  } else { panel.style.display = 'none'; }
});
</script>
</body>
</html>
"""

graph_html = (graph_template
    .replace("NODES_JSON", nodes_json)
    .replace("EDGES_JSON", edges_json))

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
    display_data = [{k: row.get(k) for k in display_cols} for row in filtered_timeline]
    st.caption(f"{len(display_data)} events")
    st.dataframe(display_data, use_container_width=True, height=400)
