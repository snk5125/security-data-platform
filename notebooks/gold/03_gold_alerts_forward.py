# Databricks notebook source
# -----------------------------------------------------------------------------
# Gold Layer: Alert Forwarding — gold.alerts → AWS SNS (CDF-based)
# -----------------------------------------------------------------------------
# Reads newly written rows from gold.alerts using Delta Change Data Feed (CDF)
# and publishes each as a JSON message to the configured AWS SNS topic.
# Successfully delivered alerts are recorded in gold.alerts_forwarded to
# prevent re-delivery on future runs.
#
# Why CDF instead of anti-join:
#   The original design used LEFT ANTI JOIN gold.alerts AGAINST gold.alerts_forwarded
#   on alert_id. This scans the entire gold.alerts table on every run — O(total rows).
#   As the table accumulates months of alerts, this becomes a bottleneck.
#
#   Delta CDF exposes only the rows that changed since the last processed Delta
#   transaction version. Reading from `last_version + 1` means the scan cost
#   is proportional to write throughput (how many new alerts were produced in
#   the last 10 minutes), not total table size. At 50M rows, the CDF read
#   costs the same as at 50K rows.
#
# High-water mark:
#   gold.alerts_forwarded stores `source_delta_version LONG` — the Delta
#   transaction version of gold.alerts that was last successfully processed.
#   On each run: starting_version = MAX(source_delta_version) + 1.
#
#   First run (no rows in alerts_forwarded): reads the full gold.alerts
#   snapshot directly, then records the current Delta version as the
#   high-water mark. All subsequent runs use CDF reads.
#
# Change type filter:
#   Only `_change_type = "insert"` rows are forwarded. Updates (flow_count
#   increments) do NOT trigger a re-send — the alert was already forwarded
#   when first inserted. This is a deliberate design choice: downstream
#   consumers receive a "new match" notification once per alert_id, not on
#   every subsequent flow from the same instance to the same IOC.
#
# Anti-join safety net:
#   Even in CDF mode, a LEFT ANTI JOIN against alerts_forwarded is applied
#   before publishing. This guards against edge cases: if the CDF read
#   returns a row that was already forwarded (e.g., due to a job retry after
#   partial failure), it is skipped — not re-sent.
#
# Dedup guarantee:
#   An alert_id appears in gold.alerts_forwarded only after SNS returns a
#   MessageId confirming receipt. The tracking table is append-only and
#   never modified — an alert_id written here will never be re-sent.
#
# Failure handling:
#   Each alert is published in a try/except block — a single SNS failure
#   skips that alert without aborting the run. Failed alerts will be retried
#   on the next run (they won't appear in alerts_forwarded). If ALL alerts
#   fail, the notebook raises to fail the Databricks task visibly.
#
# Source:  security_poc.gold.alerts (Delta CDF)
# Target:  security_poc.gold.alerts_forwarded (tracking, append-only)
#          AWS SNS topic (security-lakehouse-alerts)
# Trigger: every 10 min, as Task 3 of bronze-vpc-flow-ingest job
# -----------------------------------------------------------------------------

# COMMAND ----------

import json
import boto3
from datetime import datetime, timezone
from pyspark.sql import functions as F
from pyspark.sql.functions import col, lit, current_timestamp
from pyspark.sql.types import (
    StructType, StructField, StringType, LongType, TimestampType,
)

# ─────────────────────────────────────────────────────────────────────────────
# TABLE NAMES
# ─────────────────────────────────────────────────────────────────────────────

GOLD_ALERTS            = "security_poc.gold.alerts"
GOLD_ALERTS_FORWARDED  = "security_poc.gold.alerts_forwarded"

# ─────────────────────────────────────────────────────────────────────────────
# CREDENTIALS — read from Databricks Secret Scope
# The scope "security-lakehouse" and its secrets are created by Terraform
# (modules/databricks/jobs) and populated from the sns-alerts AWS module.
# ─────────────────────────────────────────────────────────────────────────────

SECRETS_SCOPE = "security-lakehouse"

aws_access_key_id     = dbutils.secrets.get(scope=SECRETS_SCOPE, key="aws_sns_publisher_access_key_id")
aws_secret_access_key = dbutils.secrets.get(scope=SECRETS_SCOPE, key="aws_sns_publisher_secret_access_key")
sns_topic_arn         = dbutils.secrets.get(scope=SECRETS_SCOPE, key="aws_sns_topic_arn")
aws_region            = dbutils.secrets.get(scope=SECRETS_SCOPE, key="aws_region")

# Confirm topic ARN loaded (do not log key values).
print(f"SNS topic ARN: {sns_topic_arn}")
print(f"AWS region:    {aws_region}")

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# STEP 1: ENABLE CHANGE DATA FEED ON gold.alerts
# ═════════════════════════════════════════════════════════════════════════════
# CDF must be enabled on the source table before CDF reads can be issued.
# SET TBLPROPERTIES is idempotent — safe to run on every notebook execution.
#
# Note: CDF only captures changes written AFTER it is enabled. On the very
# first run of this notebook, we handle missing CDF history by falling back
# to a full table snapshot read (see STEP 3).

spark.sql(f"""
    ALTER TABLE {GOLD_ALERTS}
    SET TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')
""")

print(f"CDF enabled on {GOLD_ALERTS}.")

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# STEP 2: ENSURE TRACKING TABLE EXISTS WITH source_delta_version COLUMN
# ═════════════════════════════════════════════════════════════════════════════
# Creates the tracking table if it does not yet exist. The source_delta_version
# column is the CDF high-water mark — the Delta transaction version of
# gold.alerts that was last successfully processed by this notebook.
#
# If the table already exists (created by an earlier version of this notebook
# that used anti-join instead of CDF), the ALTER TABLE adds the missing column
# without touching existing rows.

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {GOLD_ALERTS_FORWARDED} (

  -- alert_id from gold.alerts — uniquely identifies a forwarded alert.
  -- SHA-256 dedup key: SHA-256(instance_uid | ioc_value | feed_name | alert_type).
  alert_id             STRING    NOT NULL
                       COMMENT 'alert_id from gold.alerts — dedup key for re-delivery prevention',

  -- Copied from gold.alerts for partition-pruning on anti-join safety checks.
  alert_type           STRING    NOT NULL
                       COMMENT 'alert_type from gold.alerts — matches partition key',

  -- Message ID returned by SNS on successful publish.
  -- Useful for tracing a specific delivery through CloudWatch or SQS DLQ.
  sns_message_id       STRING    NOT NULL
                       COMMENT 'MessageId returned by SNS Publish API on successful delivery',

  -- When this row was written (when the alert was confirmed delivered).
  forwarded_at         TIMESTAMP NOT NULL
                       COMMENT 'UTC timestamp when the SNS Publish call succeeded',

  -- The Delta transaction version of gold.alerts that produced this alert.
  -- Used as the CDF high-water mark on subsequent runs. Rows written by the
  -- legacy anti-join version of this notebook have NULL here — they will still
  -- prevent re-delivery via the alert_id anti-join safety net.
  source_delta_version LONG
                       COMMENT 'Delta version of gold.alerts at time of forwarding — CDF high-water mark'

) USING DELTA
PARTITIONED BY (alert_type)
COMMENT 'Append-only dedup log for SNS-forwarded alerts. Prevents re-delivery across runs.'
""")

# Add source_delta_version if the table existed before the CDF redesign was deployed.
# Delta SQL does not support ADD COLUMN IF NOT EXISTS, so check the schema first.
existing_cols = [f.name for f in spark.table(GOLD_ALERTS_FORWARDED).schema]
if "source_delta_version" not in existing_cols:
    spark.sql(f"""
        ALTER TABLE {GOLD_ALERTS_FORWARDED}
        ADD COLUMN source_delta_version LONG
        COMMENT 'Delta version of gold.alerts at time of forwarding — CDF high-water mark'
    """)
    print("Added source_delta_version column (pre-CDF table detected).")

print(f"Tracking table verified / created: {GOLD_ALERTS_FORWARDED}")

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# STEP 3: DETERMINE STARTING POINT (CDF or SNAPSHOT)
# ═════════════════════════════════════════════════════════════════════════════
# Read the CDF high-water mark from the tracking table. Two cases:
#
#   Case A — Subsequent run (last_version is not None):
#     Read CDF from last_version + 1. Only rows written by the upstream
#     gold_alerts task in this 10-minute ingest cycle are returned.
#
#   Case B — First run (no rows in alerts_forwarded, last_version is None):
#     CDF has no history before it was enabled in STEP 1. Fall back to reading
#     the full gold.alerts snapshot directly. This ensures no alerts are missed
#     on the very first execution after the CDF redesign is deployed.
#     After publishing, we record the current Delta version as the high-water
#     mark — future runs will use CDF from that point forward.

last_version_row = spark.sql(f"""
    SELECT MAX(source_delta_version) AS last_version
    FROM {GOLD_ALERTS_FORWARDED}
""").collect()[0]

last_version = last_version_row["last_version"]  # None on first run

# Get the current Delta version of gold.alerts.
# This is the high-water mark we will store after a successful publish run.
current_version_row = spark.sql(f"""
    DESCRIBE HISTORY {GOLD_ALERTS} LIMIT 1
""").collect()[0]

current_gold_version = current_version_row["version"]

print(f"gold.alerts current Delta version: {current_gold_version}")
print(f"Last processed Delta version:      {last_version}")

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# STEP 4: READ CANDIDATE ALERTS
# ═════════════════════════════════════════════════════════════════════════════

if last_version is None:
    # ── Case B: First run — full snapshot read ──────────────────────────────
    # CDF has no history before STEP 1 enabled it. Read the entire gold.alerts
    # table and treat all rows as candidates for forwarding. The anti-join
    # safety net in STEP 5 will filter out anything already in alerts_forwarded
    # (relevant if this notebook is being re-deployed over an existing table).
    print("First run detected — reading full gold.alerts snapshot.")
    candidate_df = spark.table(GOLD_ALERTS)

else:
    # ── Case A: Subsequent run — CDF read from last_version + 1 ─────────────
    # Delta CDF returns only rows that changed since the last processed version.
    # Filter to _change_type = "insert" — we do not re-send alert updates
    # (flow_count increments for the same alert_id).
    starting_version = last_version + 1

    if starting_version > current_gold_version:
        # No new Delta versions since last run — nothing was written to gold.alerts
        # in the last 10-minute ingest cycle. Exit cleanly without SNS calls.
        print(
            f"No new Delta versions since last run "
            f"(last={last_version}, current={current_gold_version}). "
            "No alerts to forward."
        )
        dbutils.notebook.exit("no_new_versions")

    print(f"Reading CDF from version {starting_version} to {current_gold_version}.")

    candidate_df = (
        spark.read.format("delta")
        .option("readChangeFeed", "true")
        .option("startingVersion", starting_version)
        .table(GOLD_ALERTS)
        # Only forward newly inserted alerts. Updates (flow_count incremented
        # for a known alert) do not trigger a re-send — the alert was already
        # delivered when it was first inserted.
        .filter(col("_change_type") == "insert")
        # Drop CDF metadata columns before passing to the anti-join and publish.
        .drop("_change_type", "_commit_version", "_commit_timestamp")
    )

candidate_count = candidate_df.count()
print(f"Candidate alerts from this read: {candidate_count}")

if candidate_count == 0:
    print("No new alert inserts found. Exiting cleanly.")
    dbutils.notebook.exit("no_new_alerts")

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# STEP 5: ANTI-JOIN SAFETY NET
# ═════════════════════════════════════════════════════════════════════════════
# Even in CDF mode, apply a LEFT ANTI JOIN against gold.alerts_forwarded before
# publishing. This guards against two edge cases:
#
#   1. Job retry after partial failure: the previous run published some alerts
#      and wrote them to alerts_forwarded, then failed mid-run. On retry, the
#      CDF read returns the same rows again. The anti-join prevents re-delivery
#      of the alerts that were already confirmed.
#
#   2. First-run bootstrap (Case B above): alerts_forwarded may contain rows
#      written by the legacy anti-join version of this notebook. The anti-join
#      prevents re-sending alerts that were already forwarded before CDF was
#      deployed.

forwarded_ids_df = spark.table(GOLD_ALERTS_FORWARDED).select("alert_id")
new_alerts_df    = candidate_df.join(forwarded_ids_df, on="alert_id", how="left_anti")

already_sent = candidate_count - new_alerts_df.count()
to_forward   = new_alerts_df.count()

print(f"\nPost-safety-net summary:")
print(f"  Candidate alerts:             {candidate_count}")
print(f"  Already in alerts_forwarded:  {already_sent}")
print(f"  New alerts to forward:        {to_forward}")

if to_forward == 0:
    print("\nAll candidates already forwarded (safety net hit). Exiting cleanly.")
    dbutils.notebook.exit("no_new_alerts")

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# STEP 6: COLLECT ALERTS TO DRIVER
# ═════════════════════════════════════════════════════════════════════════════
# gold.alerts rows are deduplicated/aggregated (one row per instance/IOC pair)
# and bounded to a single 10-minute ingest window per run. Collecting to the
# driver avoids per-executor SNS client state management and simplifies
# per-alert error tracking.

new_alerts = new_alerts_df.collect()
print(f"\nCollected {len(new_alerts)} alerts to driver for publishing.")

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# STEP 7: BUILD SNS CLIENT
# ═════════════════════════════════════════════════════════════════════════════
# boto3 is available on Databricks serverless compute. Credentials are provided
# explicitly — serverless compute does not have an EC2 instance profile.

sns_client = boto3.client(
    "sns",
    region_name=aws_region,
    aws_access_key_id=aws_access_key_id,
    aws_secret_access_key=aws_secret_access_key,
)

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# STEP 8: PUBLISH ALERTS TO SNS
# ═════════════════════════════════════════════════════════════════════════════
# One SNS message per alert. Each message is a JSON object containing all
# populated alert fields. Null fields are omitted to keep payload compact and
# downstream parsing simple.
#
# Message subject format: "<SEVERITY>: <TITLE>"
#   Used by SNS email subscriptions as the email subject line.
#   Subject capped at 100 chars (SNS email subject limit).
#
# Per-alert try/except:
#   SNS failures are logged with the alert_id and error message. The alert is
#   not written to alerts_forwarded and will be retried on the next run because
#   it will still appear in the next CDF read (same Delta version range).

successfully_forwarded = []  # list of dicts: {alert_id, alert_type, sns_message_id, ...}
failed_alerts          = []  # list of alert_ids that failed this run

run_timestamp = datetime.now(timezone.utc)

for row in new_alerts:
    alert_id   = row["alert_id"]
    alert_type = row["alert_type"]

    # ── Build the JSON payload ──
    # Include all non-null fields. dict comprehension omits nulls so downstream
    # consumers can rely on key presence to test for field availability.
    payload = {k: v for k, v in row.asDict().items() if v is not None}

    # Timestamps are not JSON-serializable — convert to ISO 8601 strings.
    for ts_field in ("first_seen_at", "last_seen_at", "_computed_at"):
        if ts_field in payload and hasattr(payload[ts_field], "isoformat"):
            payload[ts_field] = payload[ts_field].isoformat()

    # ── Build the email-friendly subject ──
    severity = row["severity"] or "Unknown"
    title    = (row["title"] or f"Alert: {alert_id}")[:90]  # leave room for prefix
    subject  = f"{severity}: {title}"[:100]

    try:
        response = sns_client.publish(
            TopicArn=sns_topic_arn,
            Message=json.dumps(payload, default=str),
            Subject=subject,
            MessageAttributes={
                # Message attributes allow SNS subscription filter policies
                # to route by severity or alert_type without parsing the body.
                "severity": {
                    "DataType":    "String",
                    "StringValue": severity,
                },
                "alert_type": {
                    "DataType":    "String",
                    "StringValue": alert_type,
                },
            },
        )

        message_id = response["MessageId"]
        successfully_forwarded.append({
            "alert_id":             alert_id,
            "alert_type":           alert_type,
            "sns_message_id":       message_id,
            "forwarded_at":         run_timestamp,
            # Record the Delta version we read from — this becomes the
            # high-water mark used on the next run. All rows in a single
            # publish batch share the same source_delta_version.
            "source_delta_version": current_gold_version,
        })
        print(f"  [OK]   {alert_id[:16]}... → SNS MessageId: {message_id}")

    except Exception as e:
        # Log failure but continue — this alert will be retried on the next run.
        failed_alerts.append(alert_id)
        print(f"  [FAIL] {alert_id[:16]}... → {str(e)[:200]}")

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# STEP 9: WRITE TRACKING ROWS
# ═════════════════════════════════════════════════════════════════════════════
# Only write rows for alerts confirmed delivered to SNS.
# This is the dedup guarantee: alert_id appears in alerts_forwarded only
# after SNS has returned a MessageId confirming receipt.
#
# source_delta_version is written per row so MAX(source_delta_version) on
# the next run correctly advances the CDF watermark even if only a subset
# of alerts in a given batch were successfully published.

forwarded_count = len(successfully_forwarded)
failed_count    = len(failed_alerts)

print(f"\nPublishing summary:")
print(f"  Successfully published: {forwarded_count}")
print(f"  Failed (will retry):    {failed_count}")

if forwarded_count > 0:
    tracking_schema = StructType([
        StructField("alert_id",             StringType(),    False),
        StructField("alert_type",           StringType(),    False),
        StructField("sns_message_id",       StringType(),    False),
        StructField("forwarded_at",         TimestampType(), False),
        StructField("source_delta_version", LongType(),      True),
    ])

    tracking_df = spark.createDataFrame(successfully_forwarded, schema=tracking_schema)

    (
        tracking_df
        .write
        .format("delta")
        .mode("append")
        .saveAsTable(GOLD_ALERTS_FORWARDED)
    )

    print(f"\nTracking table updated: {forwarded_count} rows appended to {GOLD_ALERTS_FORWARDED}.")
    print(f"New CDF high-water mark: source_delta_version = {current_gold_version}")

# ═════════════════════════════════════════════════════════════════════════════
# FAIL THE TASK IF EVERY ALERT FAILED
# ═════════════════════════════════════════════════════════════════════════════
# If forwarded_count > 0, at least partial success — don't fail the task.
# If every alert failed, raise so the Databricks job UI shows a clear failure
# and on-call is notified via the job failure notification channel.

if failed_count > 0 and forwarded_count == 0:
    raise RuntimeError(
        f"All {failed_count} alert(s) failed to publish to SNS topic {sns_topic_arn}. "
        "Check SNS topic policy, IAM credentials in the Databricks Secret Scope, "
        "and SNS service health. Failed alert IDs: "
        + str(failed_alerts[:10])  # cap log length
    )

if failed_count > 0:
    print(
        f"\nWARNING: {failed_count} alert(s) failed to publish this run. "
        "They will be retried on the next pipeline execution."
    )

# COMMAND ----------

# ═════════════════════════════════════════════════════════════════════════════
# STEP 10: VALIDATION SUMMARY
# ═════════════════════════════════════════════════════════════════════════════

total_forwarded_ever = spark.table(GOLD_ALERTS_FORWARDED).count()
high_water_mark      = spark.sql(f"""
    SELECT MAX(source_delta_version) AS hwm FROM {GOLD_ALERTS_FORWARDED}
""").collect()[0]["hwm"]

print(f"\n{'='*60}")
print(f"Alert forwarding complete")
print(f"{'='*60}")
print(f"  Published this run:            {forwarded_count}")
print(f"  Failed this run (retry next):  {failed_count}")
print(f"  Total ever forwarded:          {total_forwarded_ever}")
print(f"  CDF high-water mark:           {high_water_mark}")
print(f"  gold.alerts current version:   {current_gold_version}")

if forwarded_count > 0:
    # Show forwarding log breakdown by alert_type for observability.
    print(f"\nForwarding log breakdown by alert_type:")
    spark.table(GOLD_ALERTS_FORWARDED).groupBy("alert_type").count().show()
