# Databricks notebook source
# -----------------------------------------------------------------------------
# Gold Layer: User Activity Timeline Materialization
# -----------------------------------------------------------------------------
# Materializes a per-investigation activity timeline into gold.user_activity_timeline.
#
# Investigation flow:
#   1. Receive a trigger_ip (malicious IP detected by upstream alert / threat intel).
#   2. If no hostname is supplied, auto-discover it by querying
#      silver.host_authentications for the most-recent auth event from trigger_ip.
#   3. Build the full unified activity timeline for that host+window via
#      build_activity_timeline() from 00_timeline_common.
#   4. Annotate every timeline row with investigation context columns:
#      investigation_id  — SHA-256(trigger_ip + "|" + hostname + "|" + trigger_time)
#                          Stable dedup key for re-runs against the same investigation.
#      trigger_ip        — The IP that initiated the investigation.
#      trigger_time      — The earliest auth event from trigger_ip in the window.
#      trigger_user      — The user who authenticated from trigger_ip.
#   5. MERGE the annotated rows into gold.user_activity_timeline on event_id,
#      creating the table on first run.
#
# Table schema (gold.user_activity_timeline):
#   All unified timeline columns from build_activity_timeline() PLUS:
#     investigation_id  STRING  — stable identifier for this investigation run
#     trigger_ip        STRING  — the malicious IP that triggered the investigation
#     trigger_time      TIMESTAMP — earliest auth event from trigger_ip
#     trigger_user      STRING  — the user authenticated from trigger_ip
#
# MERGE dedup key: event_id
#   Re-running for the same trigger_ip/window is safe — existing events are
#   updated with the latest investigation context; net-new events are inserted.
#
# Source:  security_poc.silver.host_authentications (trigger discovery)
#          build_activity_timeline() — all 4 silver host tables
# Target:  security_poc.gold.user_activity_timeline
# Trigger: on-demand or chained from a downstream alert job
# -----------------------------------------------------------------------------

# COMMAND ----------

# %run must be the first executable statement so that build_activity_timeline,
# compute_relevance, discover_identity_chain, and derive_graph are defined
# before any other cell references them.
# MAGIC %run ./00_timeline_common

# COMMAND ----------

# =============================================================================
# CELL 1 — Parameters and hostname auto-discovery
# =============================================================================

from datetime import datetime, timedelta, timezone
from pyspark.sql import functions as F
from pyspark.sql.functions import col, lit, sha2, concat

dbutils.widgets.text("trigger_ip",        "",    "Trigger IP (required)")
dbutils.widgets.text("hostname",          "",    "Hostname (optional — auto-discovered)")
dbutils.widgets.text("time_window_hours", "24",  "Time window (hours back from now)")
dbutils.widgets.text("catalog_name",      "security_poc", "Unity Catalog name")

TRIGGER_IP        = dbutils.widgets.get("trigger_ip").strip()
HOSTNAME          = dbutils.widgets.get("hostname").strip()
TIME_WINDOW_HOURS = int(dbutils.widgets.get("time_window_hours"))
CATALOG           = dbutils.widgets.get("catalog_name").strip()

if not TRIGGER_IP:
    raise ValueError("trigger_ip widget is required but was not provided.")

# Compute the investigation window relative to now (UTC)
end_time   = datetime.now(timezone.utc)
start_time = end_time - timedelta(hours=TIME_WINDOW_HOURS)

print(f"trigger_ip:        {TRIGGER_IP}")
print(f"time_window_hours: {TIME_WINDOW_HOURS}")
print(f"window:            {start_time.isoformat()} → {end_time.isoformat()}")
print(f"catalog:           {CATALOG}")

# --- Auto-discover hostname and trigger context from silver.host_authentications ---
trigger_user = None
trigger_time = None

try:
    auth_table = spark.table(f"{CATALOG}.silver.host_authentications")

    trigger_events = (
        auth_table
        .filter(
            (col("source_ip") == TRIGGER_IP) &
            col("time").between(start_time, end_time)
        )
        .orderBy("time")
    )

    trigger_rows = trigger_events.limit(1).collect()

    if trigger_rows:
        row = trigger_rows[0]
        trigger_user = row["user"]
        trigger_time = row["time"]

        if not HOSTNAME:
            HOSTNAME = row["hostname"]
            print(f"  Auto-discovered hostname: {HOSTNAME}")

        print(f"  trigger_user: {trigger_user}")
        print(f"  trigger_time: {trigger_time}")
    else:
        print(f"  WARNING: No auth events found for source_ip={TRIGGER_IP} in window.")
        print(f"  Proceeding without trigger context; relevance scoring will use defaults.")

except Exception as e:
    print(f"  WARNING: Could not query silver.host_authentications: {e}")
    print(f"  Proceeding without trigger context.")

if not HOSTNAME:
    raise ValueError(
        "hostname could not be auto-discovered from silver.host_authentications "
        "and was not supplied via widget. Provide hostname explicitly."
    )

print(f"\nhostname resolved to: {HOSTNAME}")

# COMMAND ----------

# =============================================================================
# CELL 2 — Build the unified activity timeline
# =============================================================================

print(f"Building activity timeline for host '{HOSTNAME}' ...")
print(f"  window: {start_time} → {end_time}")

timeline_df = build_activity_timeline(
    spark      = spark,
    catalog    = CATALOG,
    hostname   = HOSTNAME,
    start_time = start_time,
    end_time   = end_time,
    trigger_user = trigger_user,
    trigger_ip   = TRIGGER_IP,
)

if timeline_df is None:
    raise RuntimeError(
        "build_activity_timeline returned None — no silver data found "
        f"for hostname={HOSTNAME} in the given window. "
        "Check that the silver host telemetry tables are populated."
    )

event_count = timeline_df.count()
print(f"  Timeline rows: {event_count}")

# COMMAND ----------

# =============================================================================
# CELL 3 — Add investigation context columns
# =============================================================================
# investigation_id is stable across re-runs for the same trigger_ip + hostname +
# trigger_time combination. Using "|" as a separator avoids accidental collisions
# where string concatenation without a delimiter could produce the same hash for
# different inputs (e.g., "a" + "bc" vs "ab" + "c").

import hashlib

# Compute a Python-side investigation_id for logging; the Spark column uses sha2
# for efficiency over large DataFrames.
_id_input = f"{TRIGGER_IP}|{HOSTNAME}|{str(trigger_time) if trigger_time else ''}"
investigation_id_python = hashlib.sha256(_id_input.encode()).hexdigest()
print(f"investigation_id: {investigation_id_python}")

# Build the Spark Column expression for investigation_id
investigation_id_col = sha2(
    concat(
        lit(TRIGGER_IP),
        lit("|"),
        lit(HOSTNAME),
        lit("|"),
        lit(str(trigger_time) if trigger_time else ""),
    ),
    256,
)

annotated_df = (
    timeline_df
    .withColumn("investigation_id", investigation_id_col)
    .withColumn("trigger_ip",       lit(TRIGGER_IP))
    .withColumn("trigger_time",     lit(trigger_time).cast("timestamp"))
    .withColumn("trigger_user",     lit(trigger_user))
)

print(f"Investigation columns added. Schema preview:")
annotated_df.printSchema()

# COMMAND ----------

# =============================================================================
# CELL 4 — Write to gold.user_activity_timeline via MERGE on event_id
# =============================================================================
# First-run detection: if the table doesn't exist yet, write the full DataFrame
# directly (avoids constructing a MERGE against a non-existent target).
# Subsequent runs use MERGE so re-investigating the same window is idempotent:
#   - existing events are updated with fresh investigation context
#   - net-new events discovered in a re-run are inserted

TARGET_TABLE = f"{CATALOG}.gold.user_activity_timeline"
TEMP_VIEW    = "timeline_staging"

def table_exists(catalog: str, schema: str, table: str) -> bool:
    """Return True if the Delta table exists in Unity Catalog."""
    try:
        spark.table(f"{catalog}.{schema}.{table}").limit(0).count()
        return True
    except Exception:
        return False

if not table_exists(CATALOG, "gold", "user_activity_timeline"):
    # First write — create the table; MERGE is unnecessary
    print(f"Table {TARGET_TABLE} does not exist — performing initial write ...")
    (
        annotated_df
        .write
        .format("delta")
        .mode("overwrite")
        .saveAsTable(TARGET_TABLE)
    )
    print(f"  Created {TARGET_TABLE} with {event_count} rows.")

else:
    # Subsequent writes — MERGE to keep the table idempotent across re-runs
    print(f"Table {TARGET_TABLE} exists — performing MERGE on event_id ...")

    annotated_df.createOrReplaceTempView(TEMP_VIEW)

    spark.sql(f"""
        MERGE INTO {TARGET_TABLE} AS target
        USING {TEMP_VIEW} AS source
        ON target.event_id = source.event_id
        WHEN MATCHED THEN
            UPDATE SET
                target.investigation_id = source.investigation_id,
                target.trigger_ip       = source.trigger_ip,
                target.trigger_time     = source.trigger_time,
                target.trigger_user     = source.trigger_user,
                target.relevance_score  = source.relevance_score,
                target.identity_chain_id = source.identity_chain_id,
                target.detail           = source.detail
        WHEN NOT MATCHED THEN
            INSERT *
    """)

    print(f"  MERGE complete.")

# Confirm final row count for this investigation_id
final_count = (
    spark.table(TARGET_TABLE)
    .filter(col("investigation_id") == investigation_id_python)
    .count()
)
print(f"  Rows for investigation_id={investigation_id_python[:12]}...: {final_count}")
print(f"\nDone. Materialized to {TARGET_TABLE}.")
