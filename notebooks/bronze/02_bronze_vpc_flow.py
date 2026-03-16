# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: VPC Flow Logs Ingestion
# -----------------------------------------------------------------------------
# Ingests raw VPC Flow Logs from workload account S3 buckets into a Delta table
# using Auto Loader (cloudFiles format).
#
# VPC Flow Logs are space-delimited text files (gzipped). Auto Loader reads them
# as text, then we split columns by the known field order defined in the flow log
# format string from Phase 4.
#
# Source format: text (gzipped) under vpc-flow-logs/AWSLogs/{account}/vpcflowlogs/
# Target table: security_poc.bronze.vpc_flow_raw
#
# Parameters (passed via job or widgets):
#   - workload_a_bucket: S3 bucket name for workload account A
#   - workload_b_bucket: S3 bucket name for workload account B
#   - checkpoint_base:   S3 path for Auto Loader checkpoints
# -----------------------------------------------------------------------------

# COMMAND ----------

dbutils.widgets.text("workload_a_bucket", "", "Workload A Bucket")
dbutils.widgets.text("workload_b_bucket", "", "Workload B Bucket")
dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")

workload_a_bucket = dbutils.widgets.get("workload_a_bucket")
workload_b_bucket = dbutils.widgets.get("workload_b_bucket")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

# COMMAND ----------

# Source paths — VPC Flow Logs are under the vpc-flow-logs/ prefix.
# Each workload account gets its own stream and checkpoint.
source_paths = {
    "workload_a": f"s3://{workload_a_bucket}/vpc-flow-logs/AWSLogs/",
    "workload_b": f"s3://{workload_b_bucket}/vpc-flow-logs/AWSLogs/",
}

checkpoint_base_vf = f"{checkpoint_base}/vpc_flow"
target_table = "security_poc.bronze.vpc_flow_raw"

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

from pyspark.sql.functions import col, split, current_timestamp

# Process each workload account sequentially with its own checkpoint.
for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_vf}/{label}"
    print(f"Ingesting {label} from {path} ...")

    try:
        # Read as text — each line is a single flow log record with space-delimited fields.
        df = (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "text")
            .option("cloudFiles.schemaLocation", checkpoint_location)
            .load(path)
        )

        # Split the single "value" column by spaces into an array.
        df_split = df.withColumn("_fields", split(col("value"), " "))

        # Map each array element to a named column.
        for i, col_name in enumerate(FLOW_LOG_COLUMNS):
            df_split = df_split.withColumn(col_name, col("_fields").getItem(i))

        # Drop intermediate columns and filter out header rows.
        df_parsed = (
            df_split
            .drop("value", "_fields")
            .filter(col("version") != "version")
        )

        # Add ingestion metadata. Unity Catalog does not support input_file_name() —
        # use the built-in _metadata.file_path column instead.
        df_enriched = df_parsed.withColumn(
            "_ingested_at", current_timestamp()
        ).withColumn(
            "_source_file", col("_metadata.file_path")
        )

        # Write to the bronze Delta table.
        (
            df_enriched.writeStream
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
