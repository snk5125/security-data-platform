# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: CloudTrail Ingestion
# -----------------------------------------------------------------------------
# Ingests raw CloudTrail management events from workload account S3 buckets
# into a Delta table using Auto Loader (cloudFiles format).
#
# Auto Loader uses directory listing mode (the default) to discover new files.
# The trigger(availableNow=True) processes all available files in one batch,
# making this suitable for scheduled job execution.
#
# Source format: JSON (gzipped) under cloudtrail/AWSLogs/{account}/CloudTrail/
# Target table: security_poc.bronze.cloudtrail_raw
#
# Parameters (passed via job or widgets):
#   - workload_a_bucket: S3 bucket name for workload account A
#   - workload_b_bucket: S3 bucket name for workload account B
#   - checkpoint_base:   S3 path for Auto Loader checkpoints
# -----------------------------------------------------------------------------

# COMMAND ----------

# Widget defaults allow interactive notebook execution with fallback values.
dbutils.widgets.text("workload_a_bucket", "", "Workload A Bucket")
dbutils.widgets.text("workload_b_bucket", "", "Workload B Bucket")
dbutils.widgets.text("checkpoint_base", "", "Checkpoint Base Path")

workload_a_bucket = dbutils.widgets.get("workload_a_bucket")
workload_b_bucket = dbutils.widgets.get("workload_b_bucket")
checkpoint_base = dbutils.widgets.get("checkpoint_base")

# COMMAND ----------

# Source paths — one per workload account. CloudTrail writes JSON files under
# the cloudtrail/AWSLogs/ prefix. Auto Loader recursively discovers all .json.gz
# files under these paths.
# Each path gets its own stream and checkpoint because Auto Loader's .load()
# accepts a single path string, not a list.
source_paths = {
    "workload_a": f"s3://{workload_a_bucket}/cloudtrail/AWSLogs/",
    "workload_b": f"s3://{workload_b_bucket}/cloudtrail/AWSLogs/",
}

# Checkpoint base for this data source — each stream appends its own suffix.
checkpoint_base_ct = f"{checkpoint_base}/cloudtrail"

# Target table in the bronze schema — both streams write to the same table.
target_table = "security_poc.bronze.cloudtrail_raw"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_ct}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql.functions import current_timestamp, col

# Process each workload account's data sequentially. Each gets its own
# Auto Loader stream with a dedicated checkpoint so file tracking is independent.
# trigger(availableNow=True) processes all available files then stops — the loop
# moves to the next path only after the current stream completes.
for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_ct}/{label}"
    print(f"Ingesting {label} from {path} ...")

    try:
        df = (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "json")
            .option("cloudFiles.inferColumnTypes", "true")
            .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
            .option("cloudFiles.schemaLocation", checkpoint_location)
            .load(path)
        )

        # Add ingestion metadata. Unity Catalog does not support input_file_name() —
        # use the built-in _metadata.file_path column instead.
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
        if "CF_EMPTY_DIR" in str(e) or "empty" in str(e).lower():
            print(f"  {label} skipped — no files found yet.")
        else:
            raise

print(f"CloudTrail ingestion complete. Rows: {spark.table(target_table).count()}")
