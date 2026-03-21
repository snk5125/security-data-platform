# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: GCP Cloud Asset Inventory Ingestion
# -----------------------------------------------------------------------------
# Ingests GCP Cloud Asset Inventory exports from workload project GCS storage
# into a Delta table using Auto Loader (cloudFiles format).
#
# Cloud Asset Inventory provides a complete snapshot of all GCP resources in a
# project/org. Exports are triggered via the Cloud Asset API ExportAssets
# method, which writes JSON files to a GCS bucket. Each JSON record represents
# a single asset with its full resource configuration.
#
# This notebook is analogous to the AWS Config notebook — it provides resource
# inventory data for enrichment in the silver and gold layers (e.g., looking up
# compute instance metadata when correlating with VPC Flow Log alerts).
#
# Unlike Cloud Audit Logs and VPC Flow Logs, Asset Inventory exports are not
# OCSF-mapped at the bronze layer. The raw asset records are preserved as-is
# with ingestion metadata, following the same pattern as the AWS Config
# notebook (04_config.py). OCSF mapping for inventory data would use the
# Device Inventory (5001) class, which is better handled at the silver layer
# where cross-cloud inventory normalization occurs.
#
# Source format: JSON under asset-inventory/
# Target table: security_poc.bronze.gcp_asset_inventory_raw
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

# Source path — Cloud Asset Inventory exports are written under the
# asset-inventory/ prefix. storage_url already includes the scheme and trailing
# slash (e.g. "gs://bucket-name/"), so path suffixes are appended directly.
source_paths = {
    "gcp_workload_a": f"{gcp_workload_a_storage_url}asset-inventory/",
}

checkpoint_base_ai = f"{checkpoint_base}/gcp_asset_inventory"
target_table = "security_poc.bronze.gcp_asset_inventory_raw"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_ai}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql.functions import current_timestamp, col, to_json, struct, lit

# =============================================================================
# INGESTION LOOP — process each workload source sequentially
# =============================================================================
# Each source gets its own Auto Loader stream with a dedicated checkpoint.
# Auto Loader reads raw Cloud Asset Inventory JSON and writes directly to
# Delta with ingestion metadata. No OCSF transformation at the bronze layer —
# inventory normalization is handled in silver.
#
# Cloud Asset Inventory JSON records have a flat-ish structure:
#   name: full resource name (//service/projects/proj/...)
#   asset_type: resource type (e.g., compute.googleapis.com/Instance)
#   resource: { data: { ... }, version: ..., discovery_document_uri: ... }
#   iam_policy: { bindings: [...] }  (if IAM policy export is enabled)
#   ancestors: [ "projects/123", "folders/456", "organizations/789" ]
#   update_time: ISO8601 timestamp of last resource modification

for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_ai}/{label}"
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

        # Enrich with ingestion metadata and source file path.
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
        if "CF_EMPTY_DIR" in str(e) or "empty" in str(e).lower():
            print(f"  {label} skipped — no files found yet.")
        else:
            raise

print(f"Asset Inventory ingestion complete. Rows: {spark.table(target_table).count()}")
