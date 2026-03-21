# Databricks notebook source
# -----------------------------------------------------------------------------
# Gold Layer: Unified Alerts Table
# -----------------------------------------------------------------------------
# Correlates VPC Flow logs against active threat intelligence indicators and
# writes matched alerts to gold.alerts — a unified, OCSF-aligned alerts table
# designed to host multiple alert types (network TI hits, AV detections, etc.).
#
# Alert types supported today:
#   ti_network — VPC Flow × security.threat_intel_network (IP/CIDR IOC match)
#
# Alert types reserved for future notebooks:
#   av_detection — endpoint AV detections (file hash, process, quarantine status)
#   ti_hash      — file hash × security.threat_intel_hash (when hash feeds added)
#   ti_dns       — DNS query × security.threat_intel_dns  (when DNS feeds added)
#
# Join strategy (two-pass):
#   Pass 1 — Exact match:
#     Equi-join VPC Flow src/dst IP against silver indicators where
#     is_network_range = False (single IPs, /32 or /128). Standard Spark
#     equi-join, no UDF required.
#
#   Pass 2 — CIDR range match:
#     Rows where is_network_range = True are broadcast to all executors, then
#     a local ip_in_network() UDF is applied per flow row to test whether the
#     flow's src or dst IP falls within the CIDR. CIDR feeds are small (a few
#     thousand rows at most), so broadcasting is safe and avoids a full cross
#     join explosion.
#
# Feodo port-filter optimization:
#   When a Feodo C2 indicator has a known dest_port, the join additionally
#   requires dst_endpoint.port = ti.dest_port. This reduces false positives
#   caused by IPs that reuse C2 ports for legitimate traffic on other ports.
#
# Dedup key (alert_id):
#   SHA-256 of (instance_uid | observable_value | feed_name | alert_type)
#   A flow from the same instance to the same IOC from the same feed is
#   represented as ONE alert row — flow_count, total_bytes, and total_packets
#   are aggregated over the lookback window. This avoids one row per flow-record
#   while still capturing volumetric context.
#
# Overwrite strategy:
#   Full recompute over lookback_days (default 30). Each run overwrites the
#   entire gold.alerts partition for alert_type = "ti_network". This avoids
#   watermark complexity and ensures alerts reflect the current state of
#   security.threat_intel_network (indicators may expire between runs).
#
# Severity mapping (confidence_score → severity):
#   3 (High)     → "Critical"
#   2 (Moderate) → "High"
#   1 (Low)      → "Medium"
#
# OCSF alignment:
#   alert_class = "detection_finding" — OCSF class for correlated detections.
#   alert_type discriminator enables partitioned writes and filtered reads.
#
# Source: security_poc.bronze.vpc_flow
#         security_poc.security.threat_intel_network
# Target: security_poc.gold.alerts (overwrite for alert_type = "ti_network")
# Trigger: daily, after silver_network task completes in threat-intel-pipeline
# -----------------------------------------------------------------------------

# COMMAND ----------

# Widget: lookback window for VPC Flow correlation.
# Increase for initial backfill; decrease for faster daily runs once steady-state.
dbutils.widgets.text("lookback_days", "30", "Lookback window (days)")
LOOKBACK_DAYS = int(dbutils.widgets.get("lookback_days"))

print(f"Lookback window: {LOOKBACK_DAYS} days")

# COMMAND ----------

import hashlib
from pyspark.sql import functions as F
from pyspark.sql.functions import (
    col, lit, sha2, concat, concat_ws, coalesce,
    current_timestamp, broadcast, when,
)
from pyspark.sql.types import BooleanType

# ─────────────────────────────────────────────────────────────────────────────
# TABLE NAMES
# Defined here directly (not via %run) to keep this notebook self-contained.
# The threat intel common notebook lives in a different workspace directory;
# a cross-directory %run dependency is fragile when paths change.
# ─────────────────────────────────────────────────────────────────────────────

BRONZE_VPC_FLOW   = "security_poc.bronze.vpc_flow"
SILVER_TI_NETWORK = "security_poc.security.threat_intel_network"
GOLD_ALERTS       = "security_poc.gold.alerts"

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# CREATE GOLD TABLE IF NOT EXISTS
# ═════════════════════════════════════════════════════════════════════════════
# Unified alerts schema — designed to accommodate multiple alert types without
# schema changes. Nullable columns (network_context, endpoint/file context) are
# left NULL for alert types that don't apply.
#
# Field groups:
#   Core        — always populated, regardless of alert_type
#   Observable  — generalizes IOC fields; maps to AV observables when alert_type=av_detection
#   Network     — populated for alert_type IN ('ti_network'); NULL for AV/hash
#   Endpoint    — populated for alert_type IN ('av_detection'); NULL for network
#   Overflow    — MAP<STRING,STRING> for additional context without schema changes

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {GOLD_ALERTS} (

  -- ── Core (always populated) ───────────────────────────────────────────────

  -- Stable dedup key: SHA-256(instance_uid | observable_value | detection_source | alert_type).
  -- Uniquely identifies one "instance saw this IOC from this source" pairing.
  alert_id         STRING       NOT NULL
                   COMMENT 'SHA-256 dedup key — unique per (instance, observable, detection_source, alert_type)',

  -- Discriminator for overwrite partitioning and downstream filtering.
  -- Values: ti_network, av_detection, ti_hash, ti_dns
  alert_type       STRING       NOT NULL
                   COMMENT 'Alert type: ti_network | av_detection | ti_hash | ti_dns',

  -- OCSF-aligned alert class.
  -- detection_finding — OCSF class_uid 2004, used for correlated detections.
  alert_class      STRING
                   COMMENT 'OCSF alert class: detection_finding',

  -- Human-readable severity derived from confidence_score.
  -- confidence 3 → Critical, 2 → High, 1 → Medium
  severity         STRING
                   COMMENT 'Critical | High | Medium | Low',

  confidence_score INT
                   COMMENT '1=Low  2=Moderate  3=High (from silver threat intel)',

  -- Alert lifecycle state. Default = Active on insert.
  -- Resolved/Suppressed states can be set by downstream SOC workflows.
  status           STRING
                   COMMENT 'Active | Expired | Resolved | Suppressed',

  title            STRING
                   COMMENT 'Human-readable alert title, e.g., "Feodo C2 Contact: 1.2.3.4 (QakBot)"',

  account_id       STRING       COMMENT 'AWS account ID from VPC Flow / endpoint telemetry',
  region           STRING       COMMENT 'AWS region',
  instance_uid     STRING       COMMENT 'EC2 instance ID (i-xxxxx)',

  -- first_seen_at/last_seen_at span the lookback window — not wall-clock discovery time.
  first_seen_at    TIMESTAMP    COMMENT 'Earliest flow time matching this alert in the lookback window',
  last_seen_at     TIMESTAMP    COMMENT 'Most recent flow time matching this alert in the lookback window',

  _computed_at     TIMESTAMP    COMMENT 'When this gold row was written',

  -- ── Observable (generalizes IOC fields) ──────────────────────────────────

  -- Observable type discriminator.
  -- ti_network: "ip" or "cidr" | av_detection: "sha256", "md5", "process"
  -- ti_hash: "sha256" | ti_dns: "domain"
  observable_type  STRING       COMMENT 'ip | cidr | domain | sha256 | md5 | process',
  observable_value STRING       COMMENT 'Normalized IOC value (IP, hash, domain, etc.)',

  -- Foreign key back to the silver table row that generated this alert.
  observable_id    STRING       COMMENT 'ioc_id from security.threat_intel_network or security.threat_intel_hash',

  threat_category  STRING       COMMENT 'c2 | compromised | reputation | malware | phishing | etc.',

  -- detection_source: feed name for TI alerts; AV engine name for AV alerts
  detection_source STRING       COMMENT 'Source: feodo_tracker | emerging_threats | ipsum | <av_engine_name>',

  -- detection_name: malware family (Feodo) or AV signature name (AV alerts); NULL for ET/IPsum
  detection_name   STRING       COMMENT 'Malware family (Feodo) or AV signature; NULL if not applicable',

  -- ── Network context (populated for ti_network; NULL for AV/hash) ─────────

  src_ip           STRING       COMMENT 'Source IP from VPC Flow (src_endpoint.ip)',
  dst_ip           STRING       COMMENT 'Destination IP from VPC Flow (dst_endpoint.ip)',
  dst_port         INT          COMMENT 'Destination port from VPC Flow (dst_endpoint.port)',

  -- Direction the flow was traveling relative to the VPC.
  -- Values derived from connection_info.direction_id: 1=ingress, 2=egress
  flow_direction   STRING       COMMENT 'ingress | egress | unknown',

  -- VPC Flow action: Allowed | Denied
  action           STRING       COMMENT 'VPC Flow action: Allowed | Denied',

  vpc_uid          STRING       COMMENT 'VPC ID from src_endpoint.vpc_uid',

  -- Which endpoint (src or dst) matched the IOC.
  matched_direction STRING      COMMENT 'src | dst — which flow endpoint matched the IOC',

  -- Whether the match was an exact IP or CIDR range match.
  match_type       STRING       COMMENT 'exact | cidr — how the IOC was matched',

  -- Aggregated across all flows in the lookback window for this alert.
  flow_count       LONG         COMMENT 'Number of VPC Flow records in the lookback window',
  total_bytes      LONG         COMMENT 'Summed traffic.bytes across matched flows',
  total_packets    LONG         COMMENT 'Summed traffic.packets across matched flows',

  -- ── Endpoint / file context (populated for av_detection; NULL for network) ──

  file_path        STRING       COMMENT 'Full file path of detected file (AV alerts)',
  file_name        STRING       COMMENT 'File name of detected file (AV alerts)',
  file_hash_sha256 STRING       COMMENT 'SHA-256 hash of detected file (AV alerts)',
  process_name     STRING       COMMENT 'Process name associated with detection (AV alerts)',
  user_name        STRING       COMMENT 'Username on endpoint at time of detection (AV alerts)',
  quarantine_status STRING      COMMENT 'quarantined | failed | not_attempted (AV alerts)',

  -- ── Overflow ──────────────────────────────────────────────────────────────
  -- Arbitrary additional context that doesn't fit the core schema.
  -- Use for feed-specific metadata or future fields not yet in the schema.
  details          MAP<STRING, STRING>
                   COMMENT 'Overflow key-value pairs for additional context'

) USING DELTA
PARTITIONED BY (alert_type)
COMMENT 'Unified alerts table — OCSF-aligned, multi-type. Partitioned by alert_type for targeted overwrites.'
""")

print(f"Gold table verified / created: {GOLD_ALERTS}")

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# UDF: ip_in_network
# ═════════════════════════════════════════════════════════════════════════════
# Defined locally in this notebook — avoids a cross-directory %run dependency
# on 00_threat_intel_common. If that common notebook is available in the same
# directory, this could be replaced with a %run; keeping it local is safer for
# a standalone gold notebook.
#
# Returns True if ip_str falls within cidr_str network.
# Used in Pass 2 (CIDR range matching) for is_network_range = True indicators.
# The TI CIDR dataset is broadcast so this UDF runs with local state per executor.

@F.udf(BooleanType())
def ip_in_network(ip_str, cidr_str):
    """
    Returns True if ip_str falls within the cidr_str network.

    All imports inside the function body — required for Databricks serverless
    compute where module-level state is not serialized to executors.
    """
    import ipaddress
    if ip_str is None or cidr_str is None:
        return False
    try:
        return (
            ipaddress.ip_address(ip_str.strip())
            in ipaddress.ip_network(cidr_str.strip(), strict=False)
        )
    except Exception:
        return False

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# LOAD SOURCE DATA
# ═════════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# VPC Flow — filter to lookback window
# _ingested_at is the Databricks write time, not flow start time. Using it here
# ensures we only scan recently loaded flow data, which keeps the join fast and
# bounded. If re-processing older data is needed, increase lookback_days.
# ─────────────────────────────────────────────────────────────────────────────
flow_df = (
    spark.table(BRONZE_VPC_FLOW)
    .filter(
        col("_ingested_at") >= F.current_timestamp() - F.expr(f"INTERVAL {LOOKBACK_DAYS} DAYS")
    )
    # Pull up nested fields used in the join and output to avoid repeated struct traversal.
    .withColumn("flow_src_ip",   col("src_endpoint.ip"))
    .withColumn("flow_dst_ip",   col("dst_endpoint.ip"))
    .withColumn("flow_dst_port", col("dst_endpoint.port"))
    .withColumn("flow_vpc_uid",  col("src_endpoint.vpc_uid"))
    .withColumn("flow_instance_uid", col("src_endpoint.instance_uid"))
    .withColumn("flow_direction",
        when(col("connection_info.direction_id") == 1, lit("ingress"))
        .when(col("connection_info.direction_id") == 2, lit("egress"))
        .otherwise(lit("unknown"))
    )
    .withColumn("flow_action",   col("action"))
    .withColumn("flow_time",     col("time"))
    .withColumn("flow_bytes",    col("traffic.bytes"))
    .withColumn("flow_packets",  col("traffic.packets"))
    .withColumn("flow_account_id", col("cloud.account.uid"))
    .withColumn("flow_region",   col("cloud.region"))
)

flow_count = flow_df.count()
print(f"VPC Flow rows in last {LOOKBACK_DAYS} days: {flow_count}")

# ─────────────────────────────────────────────────────────────────────────────
# Threat Intel — active indicators only (expires_at > now)
# Filter here once; the two-pass join will split on is_network_range.
# If the TI table doesn't exist yet (threat intel pipeline hasn't run),
# gracefully skip the correlation — no alerts to generate without IOCs.
# ─────────────────────────────────────────────────────────────────────────────
ti_table_exists = spark.catalog.tableExists(SILVER_TI_NETWORK)
if not ti_table_exists:
    print(f"WARNING: {SILVER_TI_NETWORK} does not exist yet.")
    print("  Run the threat intel pipeline to populate threat indicators.")
    print("  Skipping TI correlation — no alerts to generate.")
    ti_count = 0
    exact_count = 0
    cidr_count = 0
    ti_exact = None
    ti_cidr = None
else:
    ti_df = (
        spark.table(SILVER_TI_NETWORK)
        .filter(col("expires_at") > current_timestamp())
    )

    ti_count = ti_df.count()
    print(f"Active TI indicators: {ti_count}")

    # Split for the two-pass join strategy.
    ti_exact = ti_df.filter(col("is_network_range") == False)  # noqa: E712 — Spark Column API
    ti_cidr  = ti_df.filter(col("is_network_range") == True)   # noqa: E712

    exact_count = ti_exact.count()
    cidr_count  = ti_cidr.count()
    print(f"  Exact-match indicators (single IPs): {exact_count}")
    print(f"  CIDR range indicators:               {cidr_count}")

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# HELPER: build_alert_row
# ═════════════════════════════════════════════════════════════════════════════
# Returns a DataFrame of pre-aggregated alert rows from a flow × TI match DataFrame.
# match_df must have columns:
#   flow_* columns (from flow_df above)
#   ti columns: ioc_id, ioc_value, is_network_range, feed_name, threat_category,
#               confidence_score, dest_port, malware_family
#   matched_direction (STRING: "src" or "dst")
#   match_type        (STRING: "exact" or "cidr")

def build_alert_rows(match_df, match_direction_value, match_type_value):
    """
    Aggregate matched flow × TI rows into one alert row per
    (instance_uid, observable_value, detection_source, alert_type).

    Aggregation:
      - flow_count, total_bytes, total_packets — summed
      - first_seen_at, last_seen_at — min/max of flow_time
      - All TI fields: taken from first row (stable per ioc_id)
    """
    enriched = (
        match_df
        .withColumn("matched_direction", lit(match_direction_value))
        .withColumn("match_type",        lit(match_type_value))
        # Severity derived from confidence_score.
        .withColumn("severity",
            when(col("confidence_score") == 3, lit("Critical"))
            .when(col("confidence_score") == 2, lit("High"))
            .otherwise(lit("Medium"))
        )
        # Observable type: is_network_range=True → "cidr", else "ip".
        .withColumn("observable_type",
            when(col("is_network_range") == True, lit("cidr"))  # noqa: E712
            .otherwise(lit("ip"))
        )
        # Alert title: descriptive, includes malware family when available.
        .withColumn("title",
            when(
                col("malware_family").isNotNull(),
                concat(
                    lit("Threat Intel Network Hit: "), col("ioc_value"),
                    lit(" ("), col("malware_family"), lit(") ["),
                    col("feed_name"), lit("]")
                )
            ).otherwise(
                concat(
                    lit("Threat Intel Network Hit: "), col("ioc_value"),
                    lit(" ["), col("feed_name"), lit("]")
                )
            )
        )
        # Stable dedup key: SHA-256(instance_uid | observable_value | detection_source | alert_type)
        .withColumn("alert_id",
            sha2(
                concat_ws("|",
                    coalesce(col("flow_instance_uid"), lit("")),
                    col("ioc_value"),
                    col("feed_name"),
                    lit("ti_network"),
                ),
                256,
            )
        )
    )

    # Aggregate per (alert_id, all stable TI and flow-context fields).
    # Stable fields are included in the GROUP BY — they don't change per ioc_id.
    agg_df = (
        enriched
        .groupBy(
            "alert_id", "ioc_id", "ioc_value", "is_network_range",
            "feed_name", "threat_category", "confidence_score",
            "dest_port", "malware_family",
            "flow_src_ip", "flow_dst_ip", "flow_dst_port",
            "flow_vpc_uid", "flow_instance_uid",
            "flow_direction", "flow_action", "flow_account_id", "flow_region",
            "matched_direction", "match_type",
            "severity", "observable_type", "title",
        )
        .agg(
            F.count("*").alias("flow_count"),
            F.sum("flow_bytes").alias("total_bytes"),
            F.sum("flow_packets").alias("total_packets"),
            F.min("flow_time").alias("first_seen_at"),
            F.max("flow_time").alias("last_seen_at"),
        )
    )

    return agg_df

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# EARLY EXIT — skip correlation if no TI data or no VPC Flow data
# ═════════════════════════════════════════════════════════════════════════════
if ti_count == 0 or flow_count == 0:
    # Ensure the gold table exists even when there's nothing to write.
    print(f"\nSkipping correlation: ti_count={ti_count}, flow_count={flow_count}")
    print(f"Gold table {GOLD_ALERTS} verified. No alerts to write.")
    dbutils.notebook.exit(f"Skipped — ti_count={ti_count}, flow_count={flow_count}")

# ═════════════════════════════════════════════════════════════════════════════
# PASS 1: EXACT MATCH (single IPs — is_network_range = False)
# ═════════════════════════════════════════════════════════════════════════════
# Standard equi-joins: one for src IP matches, one for dst IP matches.
# Union the results; dedup by alert_id is handled in the aggregation step.
#
# Feodo port-filter: when dest_port IS NOT NULL, additionally require
# flow dst_endpoint.port = ti.dest_port to reduce false positives.

# ── Src IP exact match ──
exact_src_raw = (
    flow_df
    .join(ti_exact, flow_df.flow_src_ip == ti_exact.ioc_value, "inner")
    # Port filter: if this TI row has a known C2 port, require the flow's
    # dst_port to match. This avoids alerting on Feodo IPs seen on other ports.
    .filter(
        ti_exact.dest_port.isNull() | (flow_df.flow_dst_port == ti_exact.dest_port)
    )
)

# ── Dst IP exact match ──
exact_dst_raw = (
    flow_df
    .join(ti_exact, flow_df.flow_dst_ip == ti_exact.ioc_value, "inner")
    .filter(
        ti_exact.dest_port.isNull() | (flow_df.flow_dst_port == ti_exact.dest_port)
    )
)

exact_src_alerts = build_alert_rows(exact_src_raw, "src", "exact")
exact_dst_alerts = build_alert_rows(exact_dst_raw, "dst", "exact")

exact_src_count = exact_src_raw.count()
exact_dst_count = exact_dst_raw.count()
print(f"\nPass 1 — Exact match raw hits:  src={exact_src_count}  dst={exact_dst_count}")

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# PASS 2: CIDR RANGE MATCH (is_network_range = True)
# ═════════════════════════════════════════════════════════════════════════════
# CIDR feed rows are small (Emerging Threats occasionally includes /24 ranges).
# Broadcast the CIDR TI set to all executors, then apply ip_in_network() UDF.
#
# A cross-join + UDF filter is used because there is no equi-join key for
# CIDR containment. The broadcast hint prevents a full shuffle of flow_df.
# This is safe as long as ti_cidr is small (expected <1K rows in practice).

cidr_src_alerts = None
cidr_dst_alerts = None

if cidr_count > 0:
    # Broadcast the CIDR TI dataset to avoid shuffling the large flow table.
    ti_cidr_broadcast = broadcast(ti_cidr)

    # ── Src IP CIDR match ──
    cidr_src_raw = (
        flow_df
        .crossJoin(ti_cidr_broadcast)
        .filter(ip_in_network(col("flow_src_ip"), col("network_address")))
        .filter(
            ti_cidr_broadcast.dest_port.isNull() |
            (flow_df.flow_dst_port == ti_cidr_broadcast.dest_port)
        )
    )

    # ── Dst IP CIDR match ──
    cidr_dst_raw = (
        flow_df
        .crossJoin(ti_cidr_broadcast)
        .filter(ip_in_network(col("flow_dst_ip"), col("network_address")))
        .filter(
            ti_cidr_broadcast.dest_port.isNull() |
            (flow_df.flow_dst_port == ti_cidr_broadcast.dest_port)
        )
    )

    cidr_src_alerts = build_alert_rows(cidr_src_raw, "src", "cidr")
    cidr_dst_alerts = build_alert_rows(cidr_dst_raw, "dst", "cidr")

    cidr_src_count = cidr_src_raw.count()
    cidr_dst_count = cidr_dst_raw.count()
    print(f"Pass 2 — CIDR match raw hits:   src={cidr_src_count}  dst={cidr_dst_count}")
else:
    print("Pass 2 — No active CIDR indicators; skipping CIDR join.")

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# UNION ALL MATCH RESULTS
# ═════════════════════════════════════════════════════════════════════════════
# Combine exact and CIDR match DataFrames. All have the same column set from
# build_alert_rows(). Dedup by alert_id handles the rare case where the same
# instance hit the same IP via both a single-IP indicator and a CIDR indicator.

all_alert_dfs = [exact_src_alerts, exact_dst_alerts]
if cidr_src_alerts is not None:
    all_alert_dfs.extend([cidr_src_alerts, cidr_dst_alerts])

combined = all_alert_dfs[0]
for df in all_alert_dfs[1:]:
    combined = combined.union(df)

# Final dedup: if the same alert_id appears from both src and dst passes
# (i.e., a flow where src AND dst both matched the same IOC), keep the row
# with the higher flow_count. In practice this is extremely rare.
from pyspark.sql import Window
dedup_window = Window.partitionBy("alert_id").orderBy(col("flow_count").desc())
combined_deduped = (
    combined
    .withColumn("_rank", F.rank().over(dedup_window))
    .filter(col("_rank") == 1)
    .drop("_rank")
)

total_alerts = combined_deduped.count()
print(f"\nTotal deduplicated alerts to write: {total_alerts}")

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# BUILD FINAL SCHEMA — project to gold.alerts column set
# ═════════════════════════════════════════════════════════════════════════════
# Add gold-table columns not derived from the join (status, alert_class, etc.)
# and NULL-fill the columns that don't apply to ti_network alerts
# (file_path, file_name, etc. are for AV alerts only).

final_df = (
    combined_deduped
    .select(
        # ── Core ──
        col("alert_id"),
        lit("ti_network").alias("alert_type"),
        lit("detection_finding").alias("alert_class"),
        col("severity"),
        col("confidence_score"),
        lit("Active").alias("status"),
        col("title"),
        col("flow_account_id").alias("account_id"),
        col("flow_region").alias("region"),
        col("flow_instance_uid").alias("instance_uid"),
        col("first_seen_at"),
        col("last_seen_at"),
        current_timestamp().alias("_computed_at"),

        # ── Observable ──
        col("observable_type"),
        col("ioc_value").alias("observable_value"),
        col("ioc_id").alias("observable_id"),
        col("threat_category"),
        col("feed_name").alias("detection_source"),
        col("malware_family").alias("detection_name"),

        # ── Network context ──
        col("flow_src_ip").alias("src_ip"),
        col("flow_dst_ip").alias("dst_ip"),
        col("flow_dst_port").alias("dst_port"),
        col("flow_direction"),
        col("flow_action").alias("action"),
        col("flow_vpc_uid").alias("vpc_uid"),
        col("matched_direction"),
        col("match_type"),
        col("flow_count"),
        col("total_bytes"),
        col("total_packets"),

        # ── Endpoint / file context (NULL for ti_network) ──
        lit(None).cast("string").alias("file_path"),
        lit(None).cast("string").alias("file_name"),
        lit(None).cast("string").alias("file_hash_sha256"),
        lit(None).cast("string").alias("process_name"),
        lit(None).cast("string").alias("user_name"),
        lit(None).cast("string").alias("quarantine_status"),

        # ── Overflow — dest_port stored for reference on Feodo rows ──
        # Could be empty map for most rows; preserving dest_port from TI here
        # gives analysts easy access to the expected C2 port context.
        when(
            col("dest_port").isNotNull(),
            F.create_map(lit("ti_dest_port"), col("dest_port").cast("string"))
        ).otherwise(
            F.create_map()
        ).alias("details"),
    )
)

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# WRITE TO GOLD TABLE — overwrite ti_network partition
# ═════════════════════════════════════════════════════════════════════════════
# Overwrite only the alert_type = "ti_network" partition so that future
# alert types (av_detection, ti_hash, ti_dns) written by their own notebooks
# are not affected. Delta's replaceWhere option performs a targeted overwrite.
#
# replaceWhere semantics: equivalent to DELETE WHERE alert_type='ti_network'
# followed by INSERT of final_df. Atomic at the partition level.

if total_alerts > 0:
    (
        final_df
        .write
        .format("delta")
        .mode("overwrite")
        .option("replaceWhere", "alert_type = 'ti_network'")
        .saveAsTable(GOLD_ALERTS)
    )
    print(f"\nWrote {total_alerts} alerts to {GOLD_ALERTS} (alert_type = 'ti_network').")
else:
    # No matches — overwrite with empty DataFrame to clear any stale rows.
    # This is intentional: if silver TI is empty or VPC flow has no hits,
    # the partition should reflect the current state (zero active alerts).
    (
        spark.createDataFrame([], final_df.schema)
        .write
        .format("delta")
        .mode("overwrite")
        .option("replaceWhere", "alert_type = 'ti_network'")
        .saveAsTable(GOLD_ALERTS)
    )
    print(f"\nNo TI network hits in lookback window. Cleared ti_network partition in {GOLD_ALERTS}.")

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# VALIDATION SUMMARY
# ═════════════════════════════════════════════════════════════════════════════

alerts = spark.table(GOLD_ALERTS).filter(col("alert_type") == "ti_network")
alert_total = alerts.count()

print(f"\n{'='*60}")
print(f"gold.alerts (ti_network) — total rows: {alert_total}")
print(f"{'='*60}")

if alert_total > 0:
    print("\nAlerts by severity:")
    alerts.groupBy("severity").count().orderBy("severity").show()

    print("Alerts by detection_source / threat_category:")
    (
        alerts
        .groupBy("detection_source", "threat_category", "confidence_score")
        .agg(
            F.count("*").alias("alert_count"),
            F.sum("flow_count").alias("total_flows"),
        )
        .orderBy("confidence_score", ascending=False)
        .show(truncate=False)
    )

    print("Top 10 IOCs by alert count:")
    (
        alerts
        .groupBy("observable_value", "detection_name", "detection_source", "severity")
        .agg(
            F.count("*").alias("alert_rows"),
            F.sum("flow_count").alias("total_flows"),
            F.sum("total_bytes").alias("total_bytes"),
        )
        .orderBy("alert_rows", ascending=False)
        .show(10, truncate=False)
    )

    print("Match type breakdown (exact vs cidr):")
    alerts.groupBy("match_type").count().show()

    print("Flow direction breakdown (ingress vs egress):")
    alerts.groupBy("flow_direction", "matched_direction").count().show()
else:
    print("No ti_network alerts in current window.")
    print(f"  VPC Flow rows scanned: {flow_count}")
    print(f"  Active TI indicators:  {ti_count}")
    print("  Check that VPC Flow data is flowing and TI indicators are not all expired.")
