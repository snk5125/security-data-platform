# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: VPC Flow Logs Ingestion (OCSF v1.1.0)
# -----------------------------------------------------------------------------
# Ingests VPC Flow Logs from workload account S3 buckets and writes them to a
# Delta table in OCSF Network Activity (class_uid 4001) format.
#
# VPC Flow Logs are space-delimited text. Auto Loader reads them as text, then
# we parse fields by position (matching the flow log format string from Phase 4)
# and map them to OCSF Network Activity fields.
#
# Key mapping decisions:
#   - All records use activity_id=6 (Traffic) and type_uid=400106
#   - action ACCEPT→action_id 1 (Allowed), REJECT→action_id 2 (Denied)
#   - flow_direction ingress→direction_id 1, egress→direction_id 2
#   - Timestamps are Unix epoch seconds → cast to timestamp
#   - severity_id is always 1 (Informational) for flow log records
#
# Source format: text (gzipped) under vpc-flow-logs/AWSLogs/
# Target table: security_poc.bronze.vpc_flow
# OCSF version: 1.1.0
#
# Parameters (passed via job or widgets):
#   - workload_a_storage_url: Storage URL for workload account A (e.g. s3://bucket/)
#   - workload_b_storage_url: Storage URL for workload account B (e.g. s3://bucket/)
#   - checkpoint_base:        S3 path for Auto Loader checkpoints
# -----------------------------------------------------------------------------

# COMMAND ----------

# MAGIC %run ./00_ocsf_common

# COMMAND ----------

dbutils.widgets.text("workload_a_storage_url", "", "Workload A Storage URL")
dbutils.widgets.text("workload_b_storage_url", "", "Workload B Storage URL")
dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")

workload_a_storage_url = dbutils.widgets.get("workload_a_storage_url")
workload_b_storage_url = dbutils.widgets.get("workload_b_storage_url")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

# COMMAND ----------

# Source paths — VPC Flow Logs are under the vpc-flow-logs/ prefix.
# storage_url already includes the scheme and trailing slash (e.g. "s3://bucket/"),
# so path suffixes are appended directly.
source_paths = {
    "workload_a": f"{workload_a_storage_url}vpc-flow-logs/AWSLogs/",
    "workload_b": f"{workload_b_storage_url}vpc-flow-logs/AWSLogs/",
}

checkpoint_base_vf = f"{checkpoint_base}/vpc_flow"
target_table = "security_poc.bronze.vpc_flow"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_vf}")
print(f"Target:       {target_table}")

# COMMAND ----------

# VPC Flow Log field names — matches the custom log format defined in Phase 4's
# aws_flow_log resource. The order must match exactly.
FLOW_LOG_COLUMNS = [
    "version", "account_id", "interface_id", "srcaddr", "dstaddr",
    "srcport", "dstport", "protocol", "packets", "bytes",
    "start_time", "end_time", "action", "log_status",
    "vpc_id", "subnet_id", "instance_id", "tcp_flags",
    "type", "pkt_srcaddr", "pkt_dstaddr", "region", "az_id",
    "sublocation_type", "sublocation_id", "pkt_src_aws_service",
    "pkt_dst_aws_service", "flow_direction",
]

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, split, current_timestamp, lit, when, to_json, struct, expr

# =============================================================================
# OCSF TRANSFORMATION — VPC Flow Logs → OCSF Network Activity (4001)
# =============================================================================

def transform_vpc_flow_to_ocsf(df):
    """
    Transform a parsed VPC Flow Log DataFrame (one row per flow record,
    columns already split by position) into OCSF Network Activity format.
    """

    df_ocsf = df.select(
        # ── Classification ──
        lit(CLASS_NETWORK_ACTIVITY).cast("int").alias("class_uid"),
        lit(CATEGORY_NETWORK).cast("int").alias("category_uid"),
        lit(ACTIVITY_TRAFFIC).cast("int").alias("activity_id"),
        lit(CLASS_NETWORK_ACTIVITY * 100 + ACTIVITY_TRAFFIC).cast("long").alias("type_uid"),
        lit(SEVERITY_INFORMATIONAL).cast("int").alias("severity_id"),
        lit("Informational").alias("severity"),

        # ── Time ──
        # VPC Flow Logs use Unix epoch seconds for start/end times.
        # Use try_cast to handle '-' placeholder for missing values.
        F.expr("try_cast(try_cast(end_time as long) as timestamp)").alias("time"),
        F.expr("try_cast(try_cast(start_time as long) as timestamp)").alias("start_time_dt"),
        F.expr("try_cast(try_cast(end_time as long) as timestamp)").alias("end_time_dt"),

        # ── Status ──
        when(col("log_status") == "OK", lit(STATUS_SUCCESS))
        .otherwise(lit(STATUS_FAILURE))
        .cast("int").alias("status_id"),
        col("log_status").alias("status"),

        # ── Action ──
        # ACCEPT → Allowed (1), REJECT → Denied (2)
        when(col("action") == "ACCEPT", lit(ACTION_ALLOWED))
        .when(col("action") == "REJECT", lit(ACTION_DENIED))
        .otherwise(lit(ACTION_UNKNOWN))
        .cast("int").alias("action_id"),

        when(col("action") == "ACCEPT", lit("Allowed"))
        .when(col("action") == "REJECT", lit("Denied"))
        .otherwise(lit("Unknown"))
        .alias("action"),

        # ── Source endpoint ──
        # VPC Flow Logs use '-' for missing values (e.g., ICMP has no port).
        # Use try_cast to safely handle these, returning null instead of error.
        F.struct(
            col("srcaddr").alias("ip"),
            F.expr("try_cast(srcport as int)").alias("port"),
            col("instance_id").alias("instance_uid"),
            col("interface_id").alias("interface_uid"),
            col("vpc_id").alias("vpc_uid"),
            col("subnet_id").alias("subnet_uid"),
        ).alias("src_endpoint"),

        # ── Destination endpoint ──
        F.struct(
            col("dstaddr").alias("ip"),
            F.expr("try_cast(dstport as int)").alias("port"),
        ).alias("dst_endpoint"),

        # ── Connection info ──
        F.struct(
            F.expr("try_cast(protocol as int)").alias("protocol_num"),
            F.expr("try_cast(tcp_flags as int)").alias("tcp_flags"),
            # flow_direction: ingress → 1, egress → 2
            when(col("flow_direction") == "ingress", lit(1))
            .when(col("flow_direction") == "egress", lit(2))
            .otherwise(lit(0))
            .cast("int").alias("direction_id"),
            # type field indicates IPv4 or IPv6
            when(col("type") == "IPv4", lit("4"))
            .when(col("type") == "IPv6", lit("6"))
            .otherwise(col("type"))
            .alias("protocol_ver"),
        ).alias("connection_info"),

        # ── Traffic metrics ──
        F.struct(
            F.expr("try_cast(bytes as long)").alias("bytes"),
            F.expr("try_cast(packets as long)").alias("packets"),
        ).alias("traffic"),

        # ── Cloud context ──
        ocsf_cloud(col("region"), col("account_id")).alias("cloud"),

        # ── Metadata ──
        ocsf_metadata("VPC Flow Logs", "VPC Flow Logs").alias("metadata"),

        # ── Unmapped — fields without direct OCSF mapping ──
        F.map_from_arrays(
            F.array(
                lit("az_id"),
                lit("sublocation_type"),
                lit("sublocation_id"),
                lit("pkt_src_aws_service"),
                lit("pkt_dst_aws_service"),
                lit("pkt_srcaddr"),
                lit("pkt_dstaddr"),
                lit("version"),
            ),
            F.array(
                col("az_id").cast("string"),
                col("sublocation_type").cast("string"),
                col("sublocation_id").cast("string"),
                col("pkt_src_aws_service").cast("string"),
                col("pkt_dst_aws_service").cast("string"),
                col("pkt_srcaddr").cast("string"),
                col("pkt_dstaddr").cast("string"),
                col("version").cast("string"),
            ),
        ).alias("unmapped"),

        # ── Raw data — the original space-delimited record as JSON ──
        to_json(struct([col(c) for c in FLOW_LOG_COLUMNS])).alias("raw_data"),

        # ── Ingestion metadata (project convention) ──
        current_timestamp().alias("_ingested_at"),
        col("_source_file"),
    )

    return df_ocsf

# COMMAND ----------

# =============================================================================
# INGESTION LOOP — read raw text, parse columns, apply OCSF, write Delta
# =============================================================================

for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_vf}/{label}"
    print(f"Ingesting {label} from {path} ...")

    try:
        # Read as text — each line is a single flow log record.
        df = (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "text")
            .option("cloudFiles.schemaLocation", checkpoint_location)
            .load(path)
        )

        # Split the single "value" column by spaces into an array, then map
        # each element to a named column by position.
        df_split = df.withColumn("_fields", split(col("value"), " "))
        for i, col_name in enumerate(FLOW_LOG_COLUMNS):
            df_split = df_split.withColumn(col_name, col("_fields").getItem(i))

        # Drop intermediate columns, filter out header rows, and capture
        # the source file path before transformation drops _metadata.
        df_parsed = (
            df_split
            .drop("value", "_fields")
            .filter(col("version") != "version")
            .withColumn("_source_file", col("_metadata.file_path"))
        )

        # Apply OCSF transformation.
        ocsf_df = transform_vpc_flow_to_ocsf(df_parsed)

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

print(f"VPC Flow Logs ingestion complete. Rows: {spark.table(target_table).count()}")
