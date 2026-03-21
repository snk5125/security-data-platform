# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: GCP VPC Flow Log Ingestion (OCSF v1.1.0)
# -----------------------------------------------------------------------------
# Ingests GCP VPC Flow Logs from workload project GCS storage and writes them
# to a Delta table in OCSF Network Activity (class_uid 4001) format.
#
# GCP VPC Flow Logs exported via Cloud Logging sinks arrive as JSON files with
# a Cloud Logging envelope. The flow record fields are nested under
# jsonPayload.connection (for the 5-tuple) and jsonPayload (for bytes/packets).
#
# Key field mappings:
#   - jsonPayload.connection.src_ip    -> src_endpoint.ip
#   - jsonPayload.connection.dest_ip   -> dst_endpoint.ip
#   - jsonPayload.connection.src_port  -> src_endpoint.port
#   - jsonPayload.connection.dest_port -> dst_endpoint.port
#   - jsonPayload.connection.protocol  -> connection_info.protocol_num
#   - jsonPayload.bytes_sent           -> traffic.bytes
#   - jsonPayload.packets_sent         -> traffic.packets
#
# Key mapping decisions:
#   - All records use activity_id=6 (Traffic) and type_uid=400106
#   - severity_id is always 1 (Informational) for flow log records
#   - src_endpoint.ip and dst_endpoint.ip match the OCSF fields used by the
#     gold_alerts notebook for cross-cloud threat intel correlation
#   - This feeds into the existing 02_gold_alerts.py pipeline via OCSF
#     Network Activity 4001 normalization (same as AWS VPC and Azure VNet)
#
# Source format: JSON under compute.googleapis.com/vpc_flows/
# Target table: security_poc.bronze.gcp_vpc_flow_raw
# OCSF version: 1.1.0
#
# Parameters (passed via job or widgets):
#   - gcp_workload_a_storage_url: Storage URL for GCP workload A
#                                 (e.g. gs://bucket-name/)
#   - checkpoint_base:            Path for Auto Loader checkpoints
# -----------------------------------------------------------------------------

# COMMAND ----------

# MAGIC %run ./00_gcp_common

# COMMAND ----------

dbutils.widgets.text("gcp_workload_a_storage_url", "", "GCP Workload A Storage URL")
dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")

gcp_workload_a_storage_url = dbutils.widgets.get("gcp_workload_a_storage_url")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

# COMMAND ----------

# Source path — VPC Flow Logs exported via Cloud Logging sinks are written
# under the compute.googleapis.com/vpc_flows/ prefix. Cloud Logging sinks
# use real slashes (not URL-encoded %2F) for GCS object prefixes.
# storage_url already includes the scheme and trailing slash
# (e.g. "gs://bucket-name/"), so path suffixes are appended directly.
source_paths = {
    "gcp_workload_a": f"{gcp_workload_a_storage_url}compute.googleapis.com/vpc_flows/",
}

checkpoint_base_vf = f"{checkpoint_base}/gcp_vpc_flow"
target_table = "security_poc.bronze.gcp_vpc_flow_raw"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_vf}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import (
    col, current_timestamp, to_json, struct, lit, when, expr
)

# =============================================================================
# OCSF TRANSFORMATION — GCP VPC Flow Logs -> OCSF Network Activity (4001)
# =============================================================================
# GCP VPC Flow Logs in Cloud Logging format have a simpler structure than
# Azure VNet Flow Logs — each JSON record is a single flow record (no nested
# arrays to explode). The connection 5-tuple is under jsonPayload.connection.

def transform_vpc_flow_to_ocsf(df):
    """
    Transform a raw GCP VPC Flow Log DataFrame into OCSF Network Activity
    format.

    Produces the same OCSF fields as the AWS VPC Flow Log and Azure VNet Flow
    Log notebooks (src_endpoint.ip, dst_endpoint.ip, dst_endpoint.port) so the
    gold_alerts notebook can correlate across all three clouds.
    """

    # Extract project ID from resource.labels for cloud.account.uid.
    df_with_project = df.withColumn(
        "_project_id",
        F.coalesce(col("resource.labels.project_id"), lit("unknown"))
    )

    df_ocsf = df_with_project.select(
        # ── Classification ──
        lit(CLASS_NETWORK_ACTIVITY).cast("int").alias("class_uid"),
        lit(CATEGORY_NETWORK).cast("int").alias("category_uid"),
        lit(ACTIVITY_TRAFFIC).cast("int").alias("activity_id"),
        lit(CLASS_NETWORK_ACTIVITY * 100 + ACTIVITY_TRAFFIC).cast("long").alias("type_uid"),
        lit(SEVERITY_INFORMATIONAL).cast("int").alias("severity_id"),
        lit("Informational").alias("severity"),

        # ── Time ──
        # Use the Cloud Logging timestamp as the event time.
        # Fall back to receiveTimestamp if timestamp is missing.
        F.coalesce(
            col("timestamp").cast("timestamp"),
            col("receiveTimestamp").cast("timestamp"),
        ).alias("time"),

        # ── Status ──
        # VPC Flow Logs always successfully record — status is always Success.
        lit(STATUS_SUCCESS).cast("int").alias("status_id"),
        lit("Success").alias("status"),

        # ── Action ──
        # GCP VPC Flow Logs do not have an explicit allow/deny field in the
        # standard log entry (firewall rules log separately). Default to
        # Allowed since flow logs only capture observed traffic.
        lit(ACTION_ALLOWED).cast("int").alias("action_id"),
        lit("Allowed").alias("action"),

        # ── Source endpoint ──
        # Uses the same field names (ip, port) as AWS VPC Flow and Azure VNet
        # Flow for cross-cloud correlation in gold_alerts.
        F.struct(
            col("jsonPayload.connection.src_ip").alias("ip"),
            col("jsonPayload.connection.src_port").cast("int").alias("port"),
        ).alias("src_endpoint"),

        # ── Destination endpoint ──
        F.struct(
            col("jsonPayload.connection.dest_ip").alias("ip"),
            col("jsonPayload.connection.dest_port").cast("int").alias("port"),
        ).alias("dst_endpoint"),

        # ── Connection info ──
        F.struct(
            col("jsonPayload.connection.protocol").cast("int").alias("protocol_num"),
            # GCP VPC Flow Logs include a reporter field indicating the observation
            # point: SRC = source VM, DEST = destination VM. Map to direction:
            # SRC -> Outbound (2), DEST -> Inbound (1)
            when(col("jsonPayload.reporter") == "SRC", lit(2))
            .when(col("jsonPayload.reporter") == "DEST", lit(1))
            .otherwise(lit(0))
            .cast("int").alias("direction_id"),
        ).alias("connection_info"),

        # ── Traffic metrics ──
        # GCP VPC Flow Logs provide bytes_sent and packets_sent from the
        # reporter's perspective.
        F.struct(
            F.coalesce(col("jsonPayload.bytes_sent").cast("long"), lit(0)).alias("bytes"),
            F.coalesce(col("jsonPayload.packets_sent").cast("long"), lit(0)).alias("packets"),
        ).alias("traffic"),

        # ── Cloud context ──
        # Use resource.labels.location (zone) for region; GCP VPC Flow Logs
        # include subnetwork location information.
        ocsf_cloud(
            F.coalesce(
                col("resource.labels.location"),
                lit("unknown"),
            ),
            col("_project_id"),
        ).alias("cloud"),

        # ── Metadata ──
        ocsf_metadata("VPC Flow Logs", "VPC Flow Logs").alias("metadata"),

        # ── Unmapped — fields without direct OCSF mapping ──
        F.map_from_arrays(
            F.array(
                lit("reporter"),
                lit("src_instance"),
                lit("dest_instance"),
                lit("src_vpc"),
                lit("dest_vpc"),
                lit("subnetwork_name"),
                lit("log_name"),
                lit("insert_id"),
                lit("rtt_msec"),
            ),
            F.array(
                col("jsonPayload.reporter").cast("string"),
                to_json(col("jsonPayload.src_instance")).cast("string"),
                to_json(col("jsonPayload.dest_instance")).cast("string"),
                to_json(col("jsonPayload.src_vpc")).cast("string"),
                to_json(col("jsonPayload.dest_vpc")).cast("string"),
                col("resource.labels.subnetwork_name").cast("string"),
                col("logName").cast("string"),
                col("insertId").cast("string"),
                col("jsonPayload.rtt_msec").cast("string"),
            ),
        ).alias("unmapped"),

        # ── Raw data — complete original event as JSON ──
        to_json(struct(df.columns)).alias("raw_data"),

        # ── Ingestion metadata (project convention) ──
        current_timestamp().alias("_ingested_at"),
        col("_metadata.file_path").alias("_source_file"),
    )

    return df_ocsf

# COMMAND ----------

# =============================================================================
# INGESTION LOOP — read raw JSON, apply OCSF transformation, write to Delta
# =============================================================================
# Unlike Azure VNet Flow Logs, GCP VPC Flow Logs from Cloud Logging sinks are
# already flattened — one JSON record per flow entry — so no multi-level
# explosion is needed. This allows direct streaming transformation without
# foreachBatch.

for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_vf}/{label}"
    print(f"Ingesting {label} from {path} ...")

    try:
        # Read raw VPC Flow Log JSON with schema inference.
        raw_df = (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "json")
            .option("cloudFiles.inferColumnTypes", "true")
            .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
            .option("cloudFiles.schemaLocation", checkpoint_location)
            .load(path)
        )

        # Apply OCSF transformation and write to Delta.
        ocsf_df = transform_vpc_flow_to_ocsf(raw_df)

        (
            ocsf_df.writeStream
            .format("delta")
            .outputMode("append")
            .option("checkpointLocation", checkpoint_location)
            .option("mergeSchema", "true")
            .trigger(availableNow=True)
            .toTable(target_table)
        )

        print(f"  {label} done.")
    except Exception as e:
        if "CF_EMPTY_DIR" in str(e) or "empty" in str(e).lower():
            print(f"  {label} skipped — no files found yet.")
        else:
            raise

print(f"VPC Flow Log ingestion complete. Rows: {spark.table(target_table).count()}")
