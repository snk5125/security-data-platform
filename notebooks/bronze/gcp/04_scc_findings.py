# Databricks notebook source
# -----------------------------------------------------------------------------
# Bronze Layer: GCP Security Command Center (SCC) Findings Ingestion
#               (OCSF v1.1.0)
# -----------------------------------------------------------------------------
# Ingests GCP Security Command Center findings from workload project GCS
# storage and writes them to a Delta table in OCSF Security Finding
# (class_uid 2001) format.
#
# SCC findings are exported via Cloud Functions, Pub/Sub, or BigQuery export
# and landed as JSON files in GCS. Each finding represents a security issue
# detected by SCC built-in services (Security Health Analytics, Web Security
# Scanner, Event Threat Detection, Container Threat Detection) or integrated
# third-party tools.
#
# Key mapping decisions:
#   - SCC severity (CRITICAL/HIGH/MEDIUM/LOW) -> OCSF severity_id (5/4/3/2)
#   - activity_id=1 (Create) for new/active findings
#   - finding.category maps to finding_info.types (e.g., "OPEN_FIREWALL",
#     "PUBLIC_BUCKET_ACL", "MFA_NOT_ENFORCED")
#   - finding.sourceProperties is variable per detector — preserved in unmapped
#
# This notebook is conditional — it only produces data when SCC is enabled
# in the GCP organization and findings have been exported to the workload
# storage bucket.
#
# Source format: JSON under scc-findings/
# Target table: security_poc.bronze.gcp_scc_findings_raw
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

# Source path — SCC findings exported to GCS are written under the
# scc-findings/ prefix. storage_url already includes the scheme and trailing
# slash (e.g. "gs://bucket-name/"), so path suffixes are appended directly.
source_paths = {
    "gcp_workload_a": f"{gcp_workload_a_storage_url}scc-findings/",
}

checkpoint_base_scc = f"{checkpoint_base}/gcp_scc_findings"
target_table = "security_poc.bronze.gcp_scc_findings_raw"

print(f"Source paths: {source_paths}")
print(f"Checkpoint:   {checkpoint_base_scc}")
print(f"Target:       {target_table}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import (
    col, current_timestamp, to_json, struct, lit, when, array
)

# =============================================================================
# OCSF TRANSFORMATION — SCC Findings -> OCSF Security Finding (2001)
# =============================================================================
# SCC finding JSON structure (when exported):
#   finding.name:              Full finding resource name
#   finding.parent:            Source display name (SCC source)
#   finding.category:          Finding category (e.g., "OPEN_FIREWALL")
#   finding.resourceName:      Affected GCP resource name
#   finding.state:             ACTIVE or INACTIVE
#   finding.severity:          CRITICAL, HIGH, MEDIUM, LOW, UNSPECIFIED
#   finding.eventTime:         ISO8601 timestamp
#   finding.createTime:        ISO8601 creation timestamp
#   finding.sourceProperties:  Variable detector-specific properties
#   resource.projectDisplayName: Project display name
#   resource.project:          projects/{number}
#   resource.type:             Resource type of affected resource

def transform_scc_to_ocsf(df):
    """
    Transform a raw SCC findings DataFrame into OCSF Security Finding (2001).
    """

    # Derive OCSF severity from SCC's severity string.
    severity_id_col = scc_severity_to_ocsf(
        F.coalesce(col("finding.severity"), lit("UNSPECIFIED"))
    )

    # Extract project ID from resource.project field.
    # Format is "projects/{project_number}" — extract the number.
    project_id_col = F.coalesce(
        F.element_at(F.split(col("resource.project"), "/"), 2),
        lit("unknown")
    )

    df_ocsf = df.select(
        # ── Classification ──
        lit(CLASS_SECURITY_FINDING).cast("int").alias("class_uid"),
        lit(CATEGORY_FINDINGS).cast("int").alias("category_uid"),
        # Active findings -> Create (1); Inactive -> Close (3)
        when(
            F.upper(col("finding.state")) == "INACTIVE",
            lit(ACTIVITY_FINDING_CLOSE)
        ).otherwise(lit(ACTIVITY_FINDING_CREATE))
        .cast("int").alias("activity_id"),
        compute_type_uid(
            lit(CLASS_SECURITY_FINDING),
            when(
                F.upper(col("finding.state")) == "INACTIVE",
                lit(ACTIVITY_FINDING_CLOSE)
            ).otherwise(lit(ACTIVITY_FINDING_CREATE))
        ).alias("type_uid"),
        severity_id_col.alias("severity_id"),
        severity_label(severity_id_col).alias("severity"),

        # ── Time — eventTime is the most recent finding event ──
        F.coalesce(
            col("finding.eventTime").cast("timestamp"),
            col("finding.createTime").cast("timestamp"),
        ).alias("time"),

        # ── Status — state ACTIVE means finding is open/confirmed ──
        when(
            F.upper(col("finding.state")) == "ACTIVE",
            lit(STATUS_SUCCESS)
        ).otherwise(lit(STATUS_UNKNOWN))
        .cast("int").alias("status_id"),
        F.coalesce(col("finding.state"), lit("Unknown")).alias("status"),

        # ── Finding info — core identification and description ──
        F.struct(
            col("finding.name").alias("uid"),
            col("finding.category").alias("title"),
            col("finding.description").alias("desc"),
            F.array(col("finding.category")).alias("types"),
            col("finding.createTime").cast("timestamp").alias("created_time"),
            col("finding.eventTime").cast("timestamp").alias("modified_time"),
        ).alias("finding_info"),

        # ── Resources — the GCP resource(s) involved in the finding ──
        # SCC findings target a specific GCP resource identified by
        # finding.resourceName. We parse it into a structured format.
        F.array(
            F.struct(
                col("resource.type").alias("type"),
                col("finding.resourceName").alias("name"),
                col("resource.projectDisplayName").alias("cloud_partition"),
            )
        ).alias("resources"),

        # ── Cloud context ──
        ocsf_cloud(
            # SCC findings don't always include region; use resource.location
            # if available, otherwise "global".
            F.coalesce(col("resource.location"), lit("global")),
            project_id_col,
        ).alias("cloud"),

        # ── Metadata ──
        F.struct(
            lit(OCSF_VERSION).alias("version"),
            F.struct(
                lit("GCP").alias("vendor_name"),
                lit("Security Command Center").alias("name"),
                F.struct(
                    col("finding.parent").alias("name"),
                ).alias("feature"),
            ).alias("product"),
            lit("SCC Findings").alias("log_name"),
        ).alias("metadata"),

        # ── Unmapped — variable-structure fields ──
        # sourceProperties varies per SCC detector/source. Preserve as JSON
        # rather than trying to normalize all variants.
        F.map_from_arrays(
            array(
                lit("source_properties"),
                lit("finding_class"),
                lit("mute"),
                lit("mute_update_time"),
                lit("external_uri"),
                lit("resource_name"),
                lit("finding_name"),
            ),
            array(
                to_json(col("finding.sourceProperties")).cast("string"),
                col("finding.findingClass").cast("string"),
                col("finding.mute").cast("string"),
                col("finding.muteUpdateTime").cast("string"),
                col("finding.externalUri").cast("string"),
                col("finding.resourceName").cast("string"),
                col("finding.name").cast("string"),
            ),
        ).alias("unmapped"),

        # ── Raw data — complete original finding as JSON ──
        to_json(struct(df.columns)).alias("raw_data"),

        # ── Ingestion metadata (project convention) ──
        current_timestamp().alias("_ingested_at"),
        col("_metadata.file_path").alias("_source_file"),
    )

    return df_ocsf

# COMMAND ----------

# =============================================================================
# INGESTION LOOP — process each workload source sequentially
# =============================================================================
# This notebook is conditional — if SCC is not enabled or no findings have
# been exported yet, the empty directory handler will skip gracefully.

for label, path in source_paths.items():
    checkpoint_location = f"{checkpoint_base_scc}/{label}"
    print(f"Ingesting {label} from {path} ...")

    try:
        raw_df = (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "json")
            .option("cloudFiles.inferColumnTypes", "true")
            .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
            .option("cloudFiles.schemaLocation", checkpoint_location)
            .load(path)
        )

        # Apply OCSF transformation.
        ocsf_df = transform_scc_to_ocsf(raw_df)

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
        # SCC may not be enabled or no findings exported yet.
        if "CF_EMPTY_DIR" in str(e) or "empty" in str(e).lower():
            print(f"  {label} skipped — no SCC findings files found yet.")
        else:
            raise

print(f"SCC Findings ingestion complete. Rows: {spark.table(target_table).count()}")
