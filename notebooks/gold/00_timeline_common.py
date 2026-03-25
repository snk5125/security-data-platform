# Databricks notebook source
# -----------------------------------------------------------------------------
# Gold Timeline Common — Reusable functions for activity timeline generation
# -----------------------------------------------------------------------------
# Imported via %run ./00_timeline_common by consumer notebooks.
#
# Functions:
#   build_activity_timeline(spark, catalog, hostname, start_time, end_time,
#                           trigger_user=None, trigger_ip=None)
#   compute_relevance(event_category, action, user, source_ip,
#                     trigger_user, trigger_ip, identity_chain, program)
#   discover_identity_chain(auth_df, process_df, trigger_user)
#   derive_graph(timeline_df, trigger_ip, trigger_hostname)
# -----------------------------------------------------------------------------

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import (
    col, lit, when, collect_set, array_contains, sha2, concat
)

# COMMAND ----------

# =============================================================================
# IDENTITY CHAIN DISCOVERY
# =============================================================================

def discover_identity_chain(auth_df, process_df, trigger_user):
    """
    Starting from trigger_user, follow escalation edges to find all linked
    identities. Returns a Python set of usernames.

    Escalation detection:
    1. auth_df: action='escalation' where detail contains a known identity
    2. process_df: command_line starts with sudo/su/runas
    """
    known = {trigger_user}
    max_iterations = 10  # safety limit for chained escalation

    for _ in range(max_iterations):
        prev_size = len(known)

        # Check auth escalation events
        if auth_df is not None:
            esc_rows = (
                auth_df
                .filter(col("action") == "escalation")
                .select("user", "raw_event")
                .collect()
            )
            for row in esc_rows:
                # The 'user' field is the target identity (e.g., root)
                # Check if the raw_event mentions a known identity as the source
                raw = (row["raw_event"] or "").lower()
                for identity in list(known):
                    if identity.lower() in raw:
                        known.add(row["user"])

        # Check sudo/su in process executions
        if process_df is not None:
            sudo_rows = (
                process_df
                .filter(
                    F.lower(col("command_line")).rlike(r"^(sudo|su)\s")
                )
                .select("user", "command_line")
                .collect()
            )
            for row in sudo_rows:
                if row["user"] in known:
                    # Extract target from 'su - <user>' or 'su <user>'
                    cmd = row["command_line"] or ""
                    parts = cmd.strip().split()
                    if len(parts) >= 2 and parts[0].lower() == "su":
                        target = parts[-1].lstrip("-")
                        if target and target != "-":
                            known.add(target)
                        else:
                            known.add("root")  # bare 'su' defaults to root
                    elif parts[0].lower() == "sudo":
                        known.add("root")  # sudo runs as root by default

        if len(known) == prev_size:
            break  # no new identities found

    return known

# COMMAND ----------

# =============================================================================
# RELEVANCE SCORING
# =============================================================================

def compute_relevance(trigger_user, trigger_ip, identity_chain):
    """
    Returns a Column expression computing relevance score (0-100).
    Designed to be called as: df.withColumn("relevance_score", compute_relevance(...))

    Consumers can override this function by redefining it before calling
    build_activity_timeline.

    Args:
        trigger_user: String literal — the initial authenticated user
        trigger_ip: String literal — the malicious IP
        identity_chain: Python set of usernames in the chain
    """
    chain_list = list(identity_chain)

    # Routine system programs to score low
    routine_programs = ["cron", "logrotate", "systemd", "systemd-logind",
                        "dhclient", "networkd", "resolved"]

    score = (
        # Auth from trigger IP = 100
        when(
            (col("event_category") == "authentication") & (col("source_ip") == trigger_ip),
            lit(100)
        )
        # Privilege escalation by chain member = 95
        .when(
            (col("action") == "escalation") & col("user").isin(chain_list),
            lit(95)
        )
        # Command execution by chain member = 90
        .when(
            (col("event_category") == "process_execution") & col("user").isin(chain_list),
            lit(90)
        )
        # Account change by chain member = 90
        .when(
            (col("event_category") == "account_change") & col("user").isin(chain_list),
            lit(90)
        )
        # Other auth by trigger user = 80
        .when(
            (col("event_category") == "authentication") & (col("user") == trigger_user),
            lit(80)
        )
        # File access / syscall by trigger user = 70
        .when(
            (col("event_category") == "system_event") & (col("user") == trigger_user),
            lit(70)
        )
        # System events by other chain members = 50
        .when(
            (col("event_category") == "system_event") & col("user").isin(chain_list),
            lit(50)
        )
        # Routine system events (program-based) = 10
        # Must be checked BEFORE the generic "Activity by other users" clause, because
        # cron, logrotate, and systemd are non-chain users that would otherwise score 20.
        .when(
            col("program").isNotNull() & F.lower(col("program")).isin(routine_programs),
            lit(10)
        )
        # Activity by other users = 20
        .when(
            col("user").isNotNull() & ~col("user").isin(chain_list),
            lit(20)
        )
        # Default = 15
        .otherwise(lit(15))
    ).cast("int")

    return score

# COMMAND ----------

# =============================================================================
# TIMELINE BUILDER
# =============================================================================

def build_activity_timeline(spark, catalog, hostname, start_time, end_time,
                            trigger_user=None, trigger_ip=None):
    """
    Build a unified activity timeline from all 4 silver host tables.

    Returns a DataFrame with the unified timeline schema. Does NOT write
    to a table — the caller decides whether to materialize or display.

    Args:
        spark: SparkSession
        catalog: Unity Catalog catalog name (e.g., "security_poc")
        hostname: Target hostname to investigate
        start_time: Start of time window (string or timestamp)
        end_time: End of time window (string or timestamp)
        trigger_user: Optional — the user who authenticated from the malicious IP
        trigger_ip: Optional — the malicious IP that triggered the investigation
    """
    time_filter = (col("time").between(start_time, end_time)) & (col("hostname") == hostname)

    # --- Read silver tables ---
    auth_df = None
    process_df = None
    account_df = None
    system_df = None

    try:
        auth_df = spark.table(f"{catalog}.silver.host_authentications").filter(time_filter)
    except Exception:
        print("  host_authentications not available.")

    try:
        process_df = spark.table(f"{catalog}.silver.host_process_executions").filter(time_filter)
    except Exception:
        print("  host_process_executions not available.")

    try:
        account_df = spark.table(f"{catalog}.silver.host_account_changes").filter(time_filter)
    except Exception:
        print("  host_account_changes not available.")

    try:
        system_df = spark.table(f"{catalog}.silver.host_system_events").filter(time_filter)
    except Exception:
        print("  host_system_events not available.")

    # --- Discover identity chain ---
    identity_chain = {trigger_user} if trigger_user else set()
    if trigger_user and (auth_df is not None or process_df is not None):
        identity_chain = discover_identity_chain(auth_df, process_df, trigger_user)
        print(f"  Identity chain: {identity_chain}")

    # --- Project each silver table into the unified timeline schema ---
    # Note: 'program' is included as a practical extension beyond the spec schema.
    # It enables routine-program detection in compute_relevance (cron, systemd, etc.)
    # and is useful for analysts in the graph detail panel.
    dfs = []

    if auth_df is not None:
        auth_timeline = auth_df.select(
            col("event_id"), col("time"), col("hostname"), col("os_type"),
            col("user"),
            lit("authentication").alias("event_category"),
            col("action"),
            F.concat_ws(" ", col("auth_method"), lit("from"), col("source_ip")).alias("detail"),
            col("source_ip"),
            col("status"),
            lit(None).cast("string").alias("program"),
            col("source_table"),
            col("raw_event"),
        )
        dfs.append(auth_timeline)

    if process_df is not None:
        proc_timeline = process_df.select(
            col("event_id"), col("time"), col("hostname"), col("os_type"),
            col("user"),
            lit("process_execution").alias("event_category"),
            lit("executed").alias("action"),
            col("command_line").alias("detail"),
            lit(None).cast("string").alias("source_ip"),
            col("status"),
            lit(None).cast("string").alias("program"),
            col("source_table"),
            col("raw_event"),
        )
        dfs.append(proc_timeline)

    if account_df is not None:
        acct_timeline = account_df.select(
            col("event_id"), col("time"), col("hostname"), col("os_type"),
            col("acting_user").alias("user"),
            lit("account_change").alias("event_category"),
            col("action"),
            F.concat_ws(" -> ", col("acting_user"), col("target_user")).alias("detail"),
            lit(None).cast("string").alias("source_ip"),
            lit("unknown").alias("status"),
            lit(None).cast("string").alias("program"),
            col("source_table"),
            col("raw_event"),
        )
        dfs.append(acct_timeline)

    if system_df is not None:
        sys_timeline = system_df.select(
            col("event_id"), col("time"), col("hostname"), col("os_type"),
            col("user"),
            lit("system_event").alias("event_category"),
            lit("system").alias("action"),
            col("message").alias("detail"),
            lit(None).cast("string").alias("source_ip"),
            lit("unknown").alias("status"),
            col("program"),
            col("source_table"),
            col("raw_event"),
        )
        dfs.append(sys_timeline)

    if not dfs:
        print("  No data found in any silver table for the given filter.")
        return None

    # --- UNION all sources ---
    timeline = dfs[0]
    for df in dfs[1:]:
        timeline = timeline.unionByName(df)

    # --- Add relevance scoring via compute_relevance ---
    if trigger_user and trigger_ip:
        timeline = timeline.withColumn(
            "relevance_score",
            compute_relevance(trigger_user, trigger_ip, identity_chain)
        )
    else:
        timeline = timeline.withColumn("relevance_score", lit(50).cast("int"))

    # --- Add identity chain ID ---
    chain_id = sha2(lit(",".join(sorted(identity_chain))), 256) if identity_chain else lit(None).cast("string")
    timeline = timeline.withColumn(
        "identity_chain_id",
        when(col("user").isin(list(identity_chain)), chain_id)
        .otherwise(lit(None).cast("string"))
    )

    # --- Order by time ---
    timeline = timeline.orderBy("time")

    return timeline

# COMMAND ----------

# =============================================================================
# GRAPH DERIVATION
# =============================================================================

def derive_graph(timeline_df, trigger_ip=None, trigger_hostname=None):
    """
    Transform a flat timeline DataFrame into nodes and edges for vis.js.

    Returns: (nodes_list, edges_list) as Python lists of dicts ready for
    JSON serialization into vis.js DataSets.
    """
    rows = timeline_df.collect()

    nodes = {}  # node_id -> {id, label, type, color, shape, size, ...}
    edges = []  # [{from, to, label, color, ...}]

    # Add trigger IP node if provided
    if trigger_ip:
        nodes[f"ip:{trigger_ip}"] = {
            "id": f"ip:{trigger_ip}",
            "label": trigger_ip,
            "type": "malicious_ip",
            "color": "#ff5555",
            "shape": "dot",
            "size": 30,
            "font": {"color": "white"},
        }

    # Add host node if provided
    if trigger_hostname:
        nodes[f"host:{trigger_hostname}"] = {
            "id": f"host:{trigger_hostname}",
            "label": trigger_hostname,
            "type": "host",
            "color": "#50fa7b",
            "shape": "box",
            "size": 25,
            "font": {"color": "#1a1a2e"},
        }

    user_event_counts = {}

    for row in rows:
        user = row["user"] or "unknown"
        category = row["event_category"]
        action = row["action"] or "unknown"
        detail = row["detail"] or ""
        source_ip = row["source_ip"] or ""
        relevance = row["relevance_score"] or 0
        time_str = str(row["time"])
        chain_id = row["identity_chain_id"]

        # Count events per user
        user_event_counts[user] = user_event_counts.get(user, 0) + 1

        # Ensure user node exists
        user_node_id = f"user:{user}"
        if user_node_id not in nodes:
            nodes[user_node_id] = {
                "id": user_node_id,
                "label": user,
                "type": "user",
                "color": "#bd93f9" if chain_id else "#44475a",
                "shape": "dot",
                "size": 15,
                "font": {"color": "white"},
                "in_chain": chain_id is not None,
            }

        # Create edges based on event category
        if category == "authentication" and source_ip and trigger_ip and source_ip == trigger_ip:
            # Malicious login edge
            edges.append({
                "from": f"ip:{trigger_ip}",
                "to": f"host:{trigger_hostname}" if trigger_hostname else user_node_id,
                "label": action,
                "color": {"color": "#ff5555"},
                "width": 3,
                "title": f"{time_str}: {action} as {user} from {source_ip}",
            })
            if trigger_hostname:
                edges.append({
                    "from": f"host:{trigger_hostname}",
                    "to": user_node_id,
                    "label": "authenticated",
                    "color": {"color": "#ff5555"},
                    "width": 2,
                    "title": f"{time_str}: authenticated as {user}",
                })

        elif category == "authentication" and action == "escalation":
            # Privilege escalation edge
            edges.append({
                "from": f"user:{detail.split('by ')[-1].strip() if 'by ' in detail else user}",
                "to": user_node_id,
                "label": "escalated",
                "color": {"color": "#ff79c6"},
                "dashes": True,
                "width": 2,
                "title": f"{time_str}: {detail}",
            })

        elif category == "process_execution":
            # Process execution edge
            cmd_label = detail[:40] + "..." if len(detail) > 40 else detail
            proc_node_id = f"proc:{hash(detail + time_str)}"
            nodes[proc_node_id] = {
                "id": proc_node_id,
                "label": cmd_label,
                "type": "process",
                "color": "#f1fa8c",
                "shape": "box",
                "size": max(8, min(20, relevance // 5)),
                "font": {"color": "#1a1a2e", "size": 11},
                "title": detail,
                "in_chain": chain_id is not None,
            }
            edges.append({
                "from": user_node_id,
                "to": proc_node_id,
                "label": "executed",
                "color": {"color": "#8be9fd"},
                "title": f"{time_str}: {user} executed {detail}",
            })

        elif category == "account_change":
            acct_node_id = f"acct:{detail}"
            if acct_node_id not in nodes:
                nodes[acct_node_id] = {
                    "id": acct_node_id,
                    "label": f"{action}: {detail}",
                    "type": "account_change",
                    "color": "#ff79c6",
                    "shape": "diamond",
                    "size": 15,
                    "font": {"color": "white"},
                    "in_chain": chain_id is not None,
                }
            edges.append({
                "from": user_node_id,
                "to": acct_node_id,
                "label": action,
                "color": {"color": "#f1fa8c"},
                "title": f"{time_str}: {detail}",
            })

        elif category == "system_event":
            sys_node_id = f"sys:{user}:{hash(detail + time_str)}"
            nodes[sys_node_id] = {
                "id": sys_node_id,
                "label": detail[:30] if detail else "system",
                "type": "system_event",
                "color": "#44475a",
                "shape": "dot",
                "size": 8,
                "font": {"color": "#6272a4", "size": 10},
                "in_chain": chain_id is not None,
            }
            edges.append({
                "from": user_node_id,
                "to": sys_node_id,
                "color": {"color": "#44475a"},
                "width": 1,
                "title": f"{time_str}: {detail}",
            })

    # Update user node sizes based on event counts
    for user, count in user_event_counts.items():
        node_id = f"user:{user}"
        if node_id in nodes:
            nodes[node_id]["size"] = min(40, 15 + count)

    return list(nodes.values()), edges
