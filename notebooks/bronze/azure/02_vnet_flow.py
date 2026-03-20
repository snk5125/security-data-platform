# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: Azure VNet Flow Log Ingestion (OCSF v1.1.0)
# -----------------------------------------------------------------------------
# Ingests Azure VNet Flow Logs from workload account storage and writes them
# to a Delta table in OCSF Network Activity (class_uid 4001) format.
#
# VNet Flow Logs are JSON files with deeply nested structure:
#   records[].properties.flows[].rule
#   records[].properties.flows[].flowGroups[].flowTuples
#
# Each flowTuple is a comma-separated string with 13 fields:
#   epoch,srcIP,dstIP,srcPort,dstPort,protocol,trafficFlow,trafficDecision,
#   flowState,packetsS2D,bytesS2D,packetsD2S,bytesD2S
#
# The notebook explodes these nested arrays and parses each tuple into
# individual columns before mapping to OCSF Network Activity fields.
#
# Key mapping decisions:
#   - All records use activity_id=6 (Traffic) and type_uid=400106
#   - trafficDecision A -> action_id 1 (Allowed), D -> action_id 2 (Denied)
#   - trafficFlow I -> direction_id 1 (Inbound), O -> direction_id 2 (Outbound)
#   - Timestamps are Unix epoch seconds -> cast to timestamp
#   - severity_id is always 1 (Informational) for flow log records
#   - src_endpoint.ip and dst_endpoint.ip match the OCSF fields used by the
#     gold_alerts notebook for cross-cloud correlation
#
# Source format: JSON under vnet-flow-logs/
# Target table: security_poc.bronze.vnet_flow_raw
# OCSF version: 1.1.0
#
# Parameters (passed via job or widgets):
#   - azure_workload_a_storage_url: Storage URL for Azure workload A
#                                   (e.g. abfss://container@account.dfs.core.windows.net/)
#   - checkpoint_base:              Path for Auto Loader checkpoints
# -----------------------------------------------------------------------------

# COMMAND ----------

# MAGIC %run ./00_azure_common

# COMMAND ----------

dbutils.widgets.text("azure_workload_a_storage_url", "", "Azure Workload A Storage URL")
dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")

azure_workload_a_storage_url = dbutils.widgets.get("azure_workload_a_storage_url")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

# COMMAND ----------

# Source path — VNet Flow Logs are exported under the vnet-flow-logs/ prefix.
# storage_url already includes the scheme and trailing slash
# (e.g. "abfss://container@account.dfs.core.windows.net/"), so path suffixes
# are appended directly.
source_paths = {
    "azure_workload_a": f"{azure_workload_a_storage_url}vnet-flow-logs/",
}

checkpoint_base_vf = f"{checkpoint_base}/vnet_flow"
target_table = "security_poc.bronze.vnet_flow_raw"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_vf}")
print(f"Target:       {target_table}")

# COMMAND ----------

# =============================================================================
# FLOW TUPLE FIELD DEFINITIONS — the 13 fields in each comma-separated tuple
# =============================================================================
# VNet Flow Log tuples are comma-separated strings with the following format:
#   epoch,srcIP,dstIP,srcPort,dstPort,protocol,trafficFlow,trafficDecision,
#   flowState,packetsS2D,bytesS2D,packetsD2S,bytesD2S
#
# Field descriptions:
#   epoch            — Unix epoch seconds when the flow was recorded
#   srcIP            — Source IP address
#   dstIP            — Destination IP address
#   srcPort          — Source port number
#   dstPort          — Destination port number
#   protocol         — IANA protocol number (6=TCP, 17=UDP, 1=ICMP)
#   trafficFlow      — I=Inbound, O=Outbound
#   trafficDecision  — A=Allowed, D=Denied
#   flowState        — B=Begin, C=Continuing, E=End
#   packetsS2D       — Packets from source to destination
#   bytesS2D         — Bytes from source to destination
#   packetsD2S       — Packets from destination to source
#   bytesD2S         — Bytes from destination to source

FLOW_TUPLE_FIELDS = [
    "epoch", "srcIP", "dstIP", "srcPort", "dstPort",
    "protocol", "trafficFlow", "trafficDecision", "flowState",
    "packetsS2D", "bytesS2D", "packetsD2S", "bytesD2S",
]

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import (
    col, current_timestamp, to_json, struct, lit, when, split, explode, expr
)

# =============================================================================
# NESTED JSON EXPLOSION — flatten VNet Flow Log records into one row per tuple
# =============================================================================
# VNet Flow Logs have a deeply nested structure:
#   records[] -> properties.flows[] -> flowGroups[] -> flowTuples (string)
#
# We must explode three levels of nesting and then parse each tuple string.

def explode_vnet_flow_records(df):
    """
    Flatten the nested VNet Flow Log JSON into one row per flow tuple.

    Input: DataFrame with one row per JSON file (containing a records array).
    Output: DataFrame with one row per flow tuple, with columns for:
      - record-level fields (time, resourceId, macAddress, etc.)
      - rule name from flows[] level
      - parsed tuple fields (epoch, srcIP, dstIP, etc.)
      - _source_file from Auto Loader metadata
    """

    # Step 1: Explode top-level records array (if present).
    # Azure diagnostic exports wrap events in { "records": [...] }.
    if "records" in df.columns:
        df = df.select(
            explode("records").alias("_record"),
            col("_metadata.file_path").alias("_source_file"),
        ).select("_record.*", "_source_file")
    elif "Records" in df.columns:
        df = df.select(
            explode("Records").alias("_record"),
            col("_metadata.file_path").alias("_source_file"),
        ).select("_record.*", "_source_file")
    else:
        # Already flattened — preserve source file metadata.
        df = df.withColumn("_source_file", col("_metadata.file_path"))

    # Step 2: Explode properties.flows[] — each element has a rule and flowGroups.
    df_flows = df.select(
        col("time"),
        col("resourceId"),
        col("macAddress"),
        col("_source_file"),
        explode("properties.flows").alias("_flow"),
    )

    # Step 3: Explode flowGroups[] within each flow — each group has flowTuples.
    df_groups = df_flows.select(
        col("time").alias("_record_time"),
        col("resourceId"),
        col("macAddress"),
        col("_source_file"),
        col("_flow.rule").alias("nsg_rule"),
        explode("_flow.flowGroups").alias("_group"),
    )

    # Step 4: Explode flowTuples — each is a comma-separated string.
    df_tuples = df_groups.select(
        col("_record_time"),
        col("resourceId"),
        col("macAddress"),
        col("_source_file"),
        col("nsg_rule"),
        explode("_group.flowTuples").alias("_tuple_str"),
    )

    # Step 5: Parse the comma-separated tuple into individual columns.
    parts = split(col("_tuple_str"), ",")
    for i, field_name in enumerate(FLOW_TUPLE_FIELDS):
        df_tuples = df_tuples.withColumn(field_name, parts.getItem(i))

    return df_tuples.drop("_tuple_str")

# COMMAND ----------

# =============================================================================
# OCSF TRANSFORMATION — VNet Flow Logs -> OCSF Network Activity (4001)
# =============================================================================

def transform_vnet_flow_to_ocsf(df):
    """
    Transform an exploded VNet Flow Log DataFrame (one row per flow tuple,
    with parsed tuple fields) into OCSF Network Activity format.

    Produces the same OCSF fields as the AWS VPC Flow Log notebook
    (src_endpoint.ip, dst_endpoint.ip, dst_endpoint.port) so the gold_alerts
    notebook can correlate across clouds.
    """

    # Extract subscription ID from resourceId for cloud.account.uid.
    df_with_sub = df.withColumn(
        "_subscription_id",
        F.element_at(F.split(col("resourceId"), "/"), 3)
    )

    df_ocsf = df_with_sub.select(
        # ── Classification ──
        lit(CLASS_NETWORK_ACTIVITY).cast("int").alias("class_uid"),
        lit(CATEGORY_NETWORK).cast("int").alias("category_uid"),
        lit(ACTIVITY_TRAFFIC).cast("int").alias("activity_id"),
        lit(CLASS_NETWORK_ACTIVITY * 100 + ACTIVITY_TRAFFIC).cast("long").alias("type_uid"),
        lit(SEVERITY_INFORMATIONAL).cast("int").alias("severity_id"),
        lit("Informational").alias("severity"),

        # ── Time ──
        # Use the flow tuple epoch as the event time. Fall back to the
        # record-level time if epoch is missing or unparseable.
        F.coalesce(
            expr("try_cast(try_cast(epoch as long) as timestamp)"),
            col("_record_time").cast("timestamp"),
        ).alias("time"),

        # ── Status ──
        # VNet Flow Logs always successfully record — status is always Success.
        lit(STATUS_SUCCESS).cast("int").alias("status_id"),
        lit("Success").alias("status"),

        # ── Action ──
        # trafficDecision: A=Allowed, D=Denied
        when(col("trafficDecision") == "A", lit(ACTION_ALLOWED))
        .when(col("trafficDecision") == "D", lit(ACTION_DENIED))
        .otherwise(lit(ACTION_UNKNOWN))
        .cast("int").alias("action_id"),

        when(col("trafficDecision") == "A", lit("Allowed"))
        .when(col("trafficDecision") == "D", lit("Denied"))
        .otherwise(lit("Unknown"))
        .alias("action"),

        # ── Source endpoint ──
        # Uses the same field names (ip, port) as AWS VPC Flow for
        # cross-cloud correlation in gold_alerts.
        F.struct(
            col("srcIP").alias("ip"),
            expr("try_cast(srcPort as int)").alias("port"),
            col("macAddress").alias("mac"),
        ).alias("src_endpoint"),

        # ── Destination endpoint ──
        F.struct(
            col("dstIP").alias("ip"),
            expr("try_cast(dstPort as int)").alias("port"),
        ).alias("dst_endpoint"),

        # ── Connection info ──
        F.struct(
            expr("try_cast(protocol as int)").alias("protocol_num"),
            # trafficFlow: I=Inbound (1), O=Outbound (2)
            when(col("trafficFlow") == "I", lit(1))
            .when(col("trafficFlow") == "O", lit(2))
            .otherwise(lit(0))
            .cast("int").alias("direction_id"),
        ).alias("connection_info"),

        # ── Traffic metrics ──
        # VNet Flow Logs provide bidirectional byte/packet counts.
        # Sum both directions for total, and preserve per-direction in unmapped.
        F.struct(
            (
                F.coalesce(expr("try_cast(bytesS2D as long)"), lit(0)) +
                F.coalesce(expr("try_cast(bytesD2S as long)"), lit(0))
            ).alias("bytes"),
            (
                F.coalesce(expr("try_cast(packetsS2D as long)"), lit(0)) +
                F.coalesce(expr("try_cast(packetsD2S as long)"), lit(0))
            ).alias("packets"),
        ).alias("traffic"),

        # ── Cloud context ──
        # Azure VNet Flow Logs don't include a region field in the tuple, so
        # we extract it from the resourceId if possible, otherwise "unknown".
        ocsf_cloud(
            lit("unknown"),
            F.coalesce(col("_subscription_id"), lit("unknown")),
        ).alias("cloud"),

        # ── Metadata ──
        ocsf_metadata("VNet Flow Logs", "VNet Flow Logs").alias("metadata"),

        # ── Unmapped — fields without direct OCSF mapping ──
        F.map_from_arrays(
            F.array(
                lit("nsg_rule"),
                lit("flow_state"),
                lit("mac_address"),
                lit("resource_id"),
                lit("packets_s2d"),
                lit("bytes_s2d"),
                lit("packets_d2s"),
                lit("bytes_d2s"),
                lit("traffic_flow_direction"),
            ),
            F.array(
                col("nsg_rule").cast("string"),
                col("flowState").cast("string"),
                col("macAddress").cast("string"),
                col("resourceId").cast("string"),
                col("packetsS2D").cast("string"),
                col("bytesS2D").cast("string"),
                col("packetsD2S").cast("string"),
                col("bytesD2S").cast("string"),
                col("trafficFlow").cast("string"),
            ),
        ).alias("unmapped"),

        # ── Raw data — the parsed tuple fields as JSON ──
        to_json(struct(
            [col(f) for f in FLOW_TUPLE_FIELDS]
            + [col("nsg_rule"), col("macAddress"), col("resourceId")]
        )).alias("raw_data"),

        # ── Ingestion metadata (project convention) ──
        current_timestamp().alias("_ingested_at"),
        col("_source_file"),
    )

    return df_ocsf

# COMMAND ----------

# =============================================================================
# INGESTION LOOP — read raw JSON, explode nested structure, apply OCSF, write
# =============================================================================

for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_vf}/{label}"
    print(f"Ingesting {label} from {path} ...")

    try:
        # Read raw VNet Flow Log JSON with schema inference.
        # VNet Flow Logs are JSON files with nested arrays — Auto Loader
        # infers the full nested schema.
        raw_df = (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "json")
            .option("cloudFiles.inferColumnTypes", "true")
            .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
            .option("cloudFiles.schemaLocation", checkpoint_location)
            .load(path)
        )

        # Explode the deeply nested flow log structure into one row per tuple,
        # then apply the OCSF transformation.
        # Note: foreachBatch is needed because explode operations on streaming
        # DataFrames require batch processing when the schema contains nested
        # arrays that must be exploded at multiple levels.
        def process_batch(batch_df, batch_id):
            if batch_df.count() == 0:
                return
            exploded_df = explode_vnet_flow_records(batch_df)
            ocsf_df = transform_vnet_flow_to_ocsf(exploded_df)
            ocsf_df.write.format("delta").mode("append").option(
                "mergeSchema", "true"
            ).saveAsTable(target_table)

        (
            raw_df.writeStream
            .foreachBatch(process_batch)
            .option("checkpointLocation", checkpoint_location)
            .trigger(availableNow=True)
            .start()
            .awaitTermination()
        )

        print(f"  {label} done.")
    except Exception as e:
        if "CF_EMPTY_DIR" in str(e) or "empty" in str(e).lower():
            print(f"  {label} skipped — no files found yet.")
        else:
            raise

print(f"VNet Flow Log ingestion complete. Rows: {spark.table(target_table).count()}")
