# Databricks notebook source
# -----------------------------------------------------------------------------
# Gold Layer: Investigation Graph -- Interactive vis.js Visualization
# -----------------------------------------------------------------------------
# Renders an interactive, browser-side vis.js network graph of the activity
# timeline for a given trigger IP. This is the capstone visualization for the
# user-activity-timeline feature: analysts can see exactly which identities,
# hosts, commands, and account changes are linked to a malicious IP, and
# interactively filter by event category.
#
# Progressive disclosure:
#   Nodes in the identity chain or with relevance_score >= 30 are rendered
#   immediately. Lower-relevance nodes are collapsed into a single cluster
#   node ("N events -- double-click to expand") to keep the initial view
#   clean. The cluster node is labeled for context but is not interactive
#   in the current implementation (full expansion can be added if needed).
#
# Rendering approach:
#   All graph data is serialized to JSON and injected into an inline HTML
#   document delivered via displayHTML(). The vis.js library is loaded from
#   unpkg CDN. This requires an internet-connected cluster; in air-gapped
#   environments, host the vis-network bundle on DBFS and update the <script>
#   src accordingly.
#
#   Security note: The detail panel uses innerHTML to render node metadata.
#   Node data originates exclusively from internal Delta silver tables owned
#   by this workspace -- not from external user input. The data is populated
#   by Databricks ingest notebooks and does not accept arbitrary HTML.
#   This is an intentional design choice for a trusted analyst-only environment.
#
# Source:  silver.host_authentications (trigger discovery)
#          build_activity_timeline() -- all 4 silver host tables
# Output:  displayHTML() -- inline vis.js graph (no Delta write)
# Trigger: on-demand / ad-hoc investigation
# -----------------------------------------------------------------------------

# COMMAND ----------

# %run must be the first executable statement so that build_activity_timeline,
# discover_identity_chain, compute_relevance, and derive_graph are all defined
# before any other cell references them.
# MAGIC %run ./00_timeline_common

# COMMAND ----------

# =============================================================================
# CELL 1 -- Parameters
# =============================================================================

from datetime import datetime, timedelta, timezone

dbutils.widgets.text("trigger_ip",        "",    "Trigger IP (required)")
dbutils.widgets.text("hostname",          "",    "Hostname (optional -- auto-discovered)")
dbutils.widgets.text("time_window_hours", "24",  "Time window (hours back from now)")
dbutils.widgets.text("catalog_name",      "security_poc", "Unity Catalog name")

TRIGGER_IP        = dbutils.widgets.get("trigger_ip").strip()
HOSTNAME          = dbutils.widgets.get("hostname").strip()
TIME_WINDOW_HOURS = int(dbutils.widgets.get("time_window_hours"))
CATALOG           = dbutils.widgets.get("catalog_name").strip()

if not TRIGGER_IP:
    raise ValueError("trigger_ip widget is required but was not provided.")

end_time   = datetime.now(timezone.utc)
start_time = end_time - timedelta(hours=TIME_WINDOW_HOURS)

print(f"trigger_ip:        {TRIGGER_IP}")
print(f"time_window_hours: {TIME_WINDOW_HOURS}")
print(f"window:            {start_time.isoformat()} -> {end_time.isoformat()}")
print(f"catalog:           {CATALOG}")

# COMMAND ----------

# =============================================================================
# CELL 2 -- (00_timeline_common already imported via %run above)
# =============================================================================

# This cell is intentionally a comment placeholder. build_activity_timeline,
# discover_identity_chain, compute_relevance, and derive_graph were loaded by
# the %run directive in the first cell.

print("00_timeline_common loaded.")

# COMMAND ----------

# =============================================================================
# CELL 3 -- Trigger discovery
# =============================================================================
# Query silver.host_authentications for all auth events where source_ip matches
# trigger_ip within the investigation window. Display a summary table so
# the analyst can see what triggered this investigation before the graph loads.

from pyspark.sql import functions as F
from pyspark.sql.functions import col

trigger_user = None
trigger_time = None

try:
    auth_table = spark.table(f"{CATALOG}.silver.host_authentications")

    trigger_events_df = (
        auth_table
        .filter(
            (col("source_ip") == TRIGGER_IP) &
            col("time").between(start_time, end_time)
        )
        .orderBy("time")
    )

    trigger_rows = trigger_events_df.collect()

    if trigger_rows:
        first = trigger_rows[0]
        trigger_user = first["user"]
        trigger_time = first["time"]

        if not HOSTNAME:
            HOSTNAME = first["hostname"]
            print(f"Auto-discovered hostname: {HOSTNAME}")

        print(f"trigger_user: {trigger_user}")
        print(f"trigger_time: {trigger_time}")
        print(f"\nTrigger events found ({len(trigger_rows)} total):")

        # Display the trigger events summary for analyst review
        display(trigger_events_df.select(
            "time", "hostname", "user", "action", "auth_method", "source_ip", "status"
        ))

    else:
        print(f"WARNING: No auth events found for source_ip={TRIGGER_IP} in the investigation window.")
        print("The graph will be built without trigger user context.")

except Exception as e:
    print(f"WARNING: Could not query silver.host_authentications: {e}")
    print("Proceeding without trigger context.")

if not HOSTNAME:
    raise ValueError(
        "hostname could not be auto-discovered from silver.host_authentications "
        "and was not supplied via widget. Provide hostname explicitly."
    )

print(f"\nResolved hostname: {HOSTNAME}")

# COMMAND ----------

# =============================================================================
# CELL 4 -- Build the unified activity timeline and display statistics
# =============================================================================

print(f"Building activity timeline for host '{HOSTNAME}' ...")

timeline_df = build_activity_timeline(
    spark        = spark,
    catalog      = CATALOG,
    hostname     = HOSTNAME,
    start_time   = start_time,
    end_time     = end_time,
    trigger_user = trigger_user,
    trigger_ip   = TRIGGER_IP,
)

if timeline_df is None:
    raise RuntimeError(
        "build_activity_timeline returned None -- no silver data found "
        f"for hostname={HOSTNAME} in the given window. "
        "Check that silver host telemetry tables are populated."
    )

# Materialize once for stats + graph derivation (avoids re-scanning silver tables)
# Note: no .cache()/.persist() -- not supported on serverless compute. We accept
# the recompute cost; timeline windows are small enough that this is acceptable.
total_events = timeline_df.count()

print(f"\nTimeline statistics:")
print(f"  Total events: {total_events}")

# Events by category
category_counts = (
    timeline_df
    .groupBy("event_category")
    .count()
    .orderBy("count", ascending=False)
    .collect()
)
for row in category_counts:
    print(f"  {row['event_category']:25s}: {row['count']}")

# Identity chain membership summary
chain_members = (
    timeline_df
    .filter(col("identity_chain_id").isNotNull())
    .select("user")
    .distinct()
    .collect()
)
if chain_members:
    print(f"\n  Identity chain members: {[r['user'] for r in chain_members]}")
else:
    print(f"\n  Identity chain: none discovered")

display(
    timeline_df
    .groupBy("event_category")
    .count()
    .orderBy("count", ascending=False)
)

# COMMAND ----------

# =============================================================================
# CELL 5 -- Derive the graph and apply progressive disclosure
# =============================================================================
# Progressive disclosure rules:
#   visible nodes: structural nodes (malicious_ip, host, user) always shown;
#                  event nodes shown if their owner is in identity chain OR
#                  if the node size proxy implies relevance_score >= 30.
#   collapsed nodes: all remaining event nodes.
# A single cluster node summarises the collapsed set so the analyst knows
# lower-relevance events exist and can narrow the window to explore them.

import json

nodes_list, edges_list = derive_graph(
    timeline_df       = timeline_df,
    trigger_ip        = TRIGGER_IP,
    trigger_hostname  = HOSTNAME,
)

print(f"Graph derived: {len(nodes_list)} nodes, {len(edges_list)} edges.")

# --- Progressive disclosure ---
RELEVANCE_THRESHOLD = 30

# Structural node types are always visible regardless of relevance
STRUCTURAL_TYPES = {"malicious_ip", "host", "user"}

visible_nodes   = []
collapsed_nodes = []

for node in nodes_list:
    node_type = node.get("type", "")
    if node_type in STRUCTURAL_TYPES:
        visible_nodes.append(node)
    elif node.get("in_chain", False):
        # Events owned by chain members are always visible
        visible_nodes.append(node)
    else:
        # derive_graph encodes relevance into node size for process nodes
        # (size = max(8, min(20, relevance // 5))). We invert that mapping:
        # size * 5 approximates relevance_score for threshold comparison.
        node_size = node.get("size", 0)
        effective_relevance = node_size * 5
        if effective_relevance >= RELEVANCE_THRESHOLD:
            visible_nodes.append(node)
        else:
            collapsed_nodes.append(node)

collapsed_count = len(collapsed_nodes)
print(f"  Visible nodes:   {len(visible_nodes)}")
print(f"  Collapsed nodes: {collapsed_count}")

# Build the set of visible node IDs to filter edges accordingly
visible_node_ids = {n["id"] for n in visible_nodes}

# Only include edges where both endpoints are in the visible set
visible_edges = [
    e for e in edges_list
    if e.get("from") in visible_node_ids and e.get("to") in visible_node_ids
]

# Build cluster node for collapsed events if any exist
cluster_node = None
if collapsed_count > 0:
    cluster_node = {
        "id":    "cluster:collapsed",
        "label": f"{collapsed_count} events\n(lower relevance)",
        "type":  "cluster",
        "color": "#44475a",
        "shape": "box",
        "size":  20,
        "font":  {"color": "#6272a4", "size": 11},
        "title": (
            f"{collapsed_count} lower-relevance events hidden. "
            "Narrow the time window to expand."
        ),
        "in_chain": False,
        "event_category": None,
    }

# Ensure all visible nodes have an event_category key so the JS filter logic
# can reliably test for null (structural nodes are always shown)
for node in visible_nodes:
    if "event_category" not in node:
        node["event_category"] = None

print(f"  Visible edges:   {len(visible_edges)}")

# COMMAND ----------

# =============================================================================
# CELL 6 -- Render vis.js interactive graph
# =============================================================================

# Build the final node list: visible nodes + optional cluster summary node
if cluster_node is not None:
    final_nodes = visible_nodes + [cluster_node]
else:
    final_nodes = visible_nodes

nodes_json = json.dumps(final_nodes)
edges_json = json.dumps(visible_edges)

# HTML template with NODES_JSON / EDGES_JSON placeholders.
# The detail panel uses innerHTML to render node metadata sourced exclusively
# from internal Delta silver tables (not external user input). This is safe
# in this trusted analyst-only Databricks workspace context.
html_template = (
    '<!DOCTYPE html>'
    '<html>'
    '<head>'
    '<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>'
    '<style>'
    '#graph-container { width: 100%; height: 600px; border: 1px solid #44475a; background: #1a1a2e; }'
    '#detail-panel { display: none; position: absolute; right: 10px; top: 10px; width: 300px;'
    ' background: #282a36; color: #f8f8f2; border: 1px solid #44475a; border-radius: 8px;'
    ' padding: 16px; font-family: monospace; font-size: 13px; max-height: 400px;'
    ' overflow-y: auto; z-index: 10; }'
    '.controls { display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }'
    '.filter-btn { padding: 6px 14px; border-radius: 20px; border: 1px solid #44475a;'
    ' background: #44475a; color: #f8f8f2; cursor: pointer; font-size: 13px; }'
    '.filter-btn.active { background: var(--btn-color); border-color: var(--btn-color); }'
    '</style>'
    '</head>'
    '<body style="margin:0; background:#1a1a2e; font-family:sans-serif; color:#f8f8f2;">'
    '<div class="controls">'
    '<button class="filter-btn active" style="--btn-color:#50fa7b"'
    ' onclick="toggleFilter(\'authentication\')">Auth</button>'
    '<button class="filter-btn active" style="--btn-color:#8be9fd"'
    ' onclick="toggleFilter(\'process_execution\')">Commands</button>'
    '<button class="filter-btn active" style="--btn-color:#ff79c6"'
    ' onclick="toggleFilter(\'account_change\')">Account Changes</button>'
    '<button class="filter-btn" style="--btn-color:#44475a"'
    ' onclick="toggleFilter(\'system_event\')">System Events</button>'
    '<button class="filter-btn" onclick="resetView()">Reset View</button>'
    '</div>'
    '<div style="position:relative;">'
    '<div id="graph-container"></div>'
    '<div id="detail-panel"></div>'
    '</div>'
    '<script>'
    'var allNodes = NODES_JSON;'
    'var allEdges = EDGES_JSON;'
    'var activeFilters = {authentication: true, process_execution: true, account_change: true, system_event: false};'
    'var nodes = new vis.DataSet(allNodes.filter(n => !n.event_category || n.type === \'malicious_ip\' || n.type === \'host\' || n.type === \'user\' || activeFilters[n.event_category || \'\']));'
    'var edges = new vis.DataSet(allEdges);'
    'var container = document.getElementById(\'graph-container\');'
    'var data = {nodes: nodes, edges: edges};'
    'var options = {'
    '  layout: { hierarchical: { direction: \'LR\', sortMethod: \'directed\', levelSeparation: 200, nodeSpacing: 100 } },'
    '  physics: { enabled: true, hierarchicalRepulsion: { nodeDistance: 150 } },'
    '  interaction: { hover: true, tooltipDelay: 200 },'
    '  edges: { arrows: { to: { enabled: true, scaleFactor: 0.5 } }, smooth: { type: \'cubicBezier\' } }'
    '};'
    'var network = new vis.Network(container, data, options);'
    'network.once(\'stabilized\', function() { network.setOptions({physics: false}); });'
    'network.on(\'click\', function(params) {'
    '  var panel = document.getElementById(\'detail-panel\');'
    '  if (params.nodes.length > 0) {'
    '    var nodeId = params.nodes[0];'
    '    var node = allNodes.find(n => n.id === nodeId);'
    '    if (node) {'
    '      var html = \'<h3 style="margin-top:0;color:\' + (node.color || \'#f8f8f2\') + \'">\' + (node.label || nodeId) + \'</h3>\';'
    '      html += \'<div>Type: \' + (node.type || \'unknown\') + \'</div>\';'
    '      if (node.event_category) html += \'<div>Category: \' + node.event_category + \'</div>\';'
    '      if (node.title) html += \'<div style="margin-top:8px;color:#6272a4;">\' + node.title + \'</div>\';'
    '      panel.innerHTML = html;'
    '      panel.style.display = \'block\';'
    '    }'
    '  } else { panel.style.display = \'none\'; }'
    '});'
    'function toggleFilter(category) {'
    '  activeFilters[category] = !activeFilters[category];'
    '  var btns = document.querySelectorAll(\'.filter-btn\');'
    '  btns.forEach(function(btn) {'
    '    if (btn.textContent.toLowerCase().includes(category.replace(\'_\', \' \').substring(0,4))) {'
    '      btn.classList.toggle(\'active\');'
    '    }'
    '  });'
    '  applyFilters();'
    '}'
    'function applyFilters() {'
    '  var filtered = allNodes.filter(function(n) {'
    '    if (!n.event_category) return true;'
    '    return activeFilters[n.event_category] !== false;'
    '  });'
    '  nodes.clear();'
    '  nodes.add(filtered);'
    '}'
    'function resetView() {'
    '  activeFilters = {authentication: true, process_execution: true, account_change: true, system_event: false};'
    '  document.querySelectorAll(\'.filter-btn\').forEach(function(btn, i) { btn.classList.toggle(\'active\', i < 3); });'
    '  applyFilters();'
    '  network.fit();'
    '}'
    '</script>'
    '</body>'
    '</html>'
)

# Inject the serialized graph data into the HTML template
html = html_template.replace("NODES_JSON", nodes_json).replace("EDGES_JSON", edges_json)

# Size guard -- large graphs can overwhelm the Databricks output cell renderer
html_size = len(html)
if html_size > 10_000_000:
    print(
        f"WARNING: Graph HTML is {html_size / 1_000_000:.1f}MB. "
        "Consider narrowing the time window or increasing the relevance threshold "
        "to reduce the number of visible nodes."
    )

print(f"Rendering graph: {len(final_nodes)} nodes, {len(visible_edges)} edges ({html_size / 1024:.1f}KB)")
displayHTML(html)
