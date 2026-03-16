# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: GuardDuty Findings Ingestion
# -----------------------------------------------------------------------------
# Ingests raw GuardDuty findings from workload account S3 buckets into a Delta
# table using Auto Loader (cloudFiles format).
#
# GuardDuty exports findings as JSONL (one JSON object per line, gzipped) to
# the bucket root under AWSLogs/{account}/GuardDuty/. Note: this is NOT under
# a top-level "guardduty/" prefix — the GuardDuty publishing destination writes
# directly to AWSLogs/.
#
# Source format: JSONL (gzipped) under AWSLogs/{account}/GuardDuty/
# Target table: security_poc.bronze.guardduty_raw
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

# Source paths — GuardDuty findings are under the AWSLogs/ prefix at the bucket
# root (not under a "guardduty/" prefix). We scope to the GuardDuty subdirectory
# to avoid picking up other AWSLogs data.
# Each workload account gets its own stream and checkpoint.
# Use the AWSLogs/ prefix without globs — Auto Loader recursively discovers
# files. The pathGlobFilter ensures we only pick up .jsonl.gz GuardDuty files
# and not other AWSLogs data (CloudTrail writes .json.gz, Config also .json.gz,
# but GuardDuty uses .jsonl.gz).
source_paths = {
    "workload_a": f"s3://{workload_a_bucket}/AWSLogs/",
    "workload_b": f"s3://{workload_b_bucket}/AWSLogs/",
}

checkpoint_base_gd = f"{checkpoint_base}/guardduty"
target_table = "security_poc.bronze.guardduty_raw"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_gd}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql.functions import current_timestamp, col

# Process each workload account sequentially with its own checkpoint.
for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_gd}/{label}"
    print(f"Ingesting {label} from {path} ...")

    try:
        df = (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "json")
            .option("cloudFiles.inferColumnTypes", "true")
            .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
            .option("cloudFiles.schemaLocation", checkpoint_location)
            .option("pathGlobFilter", "*.jsonl.gz")
            .load(path)
        )

        # Unity Catalog does not support input_file_name() — use _metadata.file_path.
        df_enriched = df.withColumn(
            "_ingested_at", current_timestamp()
        ).withColumn(
            "_source_file", col("_metadata.file_path")
        )

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
        # Gracefully handle empty directories — GuardDuty may not have exported
        # findings yet for all accounts.
        if "CF_EMPTY_DIR" in str(e) or "empty" in str(e).lower():
            print(f"  {label} skipped — no GuardDuty files found yet.")
        else:
            raise

print(f"GuardDuty ingestion complete. Rows: {spark.table(target_table).count()}")
