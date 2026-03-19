# Playbook: Threat Intel Pipeline — Operations and Triage

**Pipeline:** Threat Intel Alert Pipeline (two jobs: `threat-intel-pipeline` + `bronze-vpc-flow-ingest`)
**On-call trigger:** Job failure notification from Databricks, or SNS alerts stop arriving

---

## Overview

The threat intel alert pipeline has two jobs with different cadences:

| Job | Schedule | Tasks | Failure impact |
|-----|----------|-------|----------------|
| `threat-intel-pipeline` | Daily 01:00 UTC | `bronze_ingest` → `silver_network` | IOC database goes stale; alerts continue from yesterday's indicators |
| `bronze-vpc-flow-ingest` | Every 10 minutes | `ingest` → `gold_alerts` → `forward_alerts` | No new alerts forwarded to SNS; `gold.alerts` not updated |

---

## 1. Checking Pipeline Health

### Job run status in Databricks

Navigate to **Workflows → Jobs** in the Databricks workspace and check:
- `bronze-vpc-flow-ingest` — should show a successful run within the last 10 minutes
- `threat-intel-pipeline` — should show a successful run within the last 24 hours

### Quick health check queries

**Is the TI feed database fresh?**

```sql
-- Most recent bronze ingest per feed
SELECT
  feed_name,
  MAX(fetch_timestamp) AS last_fetched,
  COUNT(*) AS rows_in_last_fetch,
  TIMESTAMPDIFF(HOUR, MAX(fetch_timestamp), current_timestamp()) AS hours_since_fetch
FROM security_poc.bronze.threat_intel_raw
WHERE fetch_timestamp >= current_timestamp() - INTERVAL 2 DAYS
GROUP BY feed_name
ORDER BY last_fetched DESC
```

Expected: each feed updated within the last 25 hours. If a feed shows `hours_since_fetch > 25`, that feed's last ingest failed or was skipped.

**How many active IOCs?**

```sql
SELECT
  feed_name,
  threat_category,
  COUNT(*) AS active_iocs,
  MIN(expires_at) AS soonest_expiry,
  MAX(last_seen_at) AS freshest_seen
FROM security_poc.silver.threat_intel_network
WHERE expires_at > current_timestamp()
GROUP BY feed_name, threat_category
ORDER BY active_iocs DESC
```

Expected: ~500–2,000 rows from `feodo_tracker`, ~5,000–15,000 from `emerging_threats`, ~10,000–50,000 from `ipsum`.
Zero rows from any feed means either that feed's last run failed, or all its IOCs expired (TTL issue — see [Section 4](#4-stale-iocs)).

**Is the CDF watermark advancing?**

```sql
SELECT
  MAX(source_delta_version) AS cdf_high_water_mark,
  MAX(forwarded_at) AS last_forwarded_at,
  TIMESTAMPDIFF(MINUTE, MAX(forwarded_at), current_timestamp()) AS minutes_since_last_forward,
  COUNT(*) AS total_alerts_forwarded
FROM security_poc.gold.alerts_forwarded
```

Expected: `minutes_since_last_forward` < 15 during active hours (assuming flows are arriving).
If `minutes_since_last_forward` > 60, the forwarding pipeline is stalled.

**Current Delta version of gold.alerts:**

```sql
DESCRIBE HISTORY security_poc.gold.alerts LIMIT 5
```

Compare `version` against `cdf_high_water_mark` from above. If `version` is significantly ahead of the watermark, `forward_alerts` may be failing to write to `alerts_forwarded`.

---

## 2. Threat Intel Feed Failures

### Symptoms

- `bronze.threat_intel_raw` shows a gap in `fetch_timestamp` for one or more feeds
- `silver.threat_intel_network` IOC count drops to zero for a feed
- `bronze_ingest` task in `threat-intel-pipeline` failed

### Diagnosis

Check the `bronze_ingest` notebook output in the last failed job run (Databricks job UI → run → task output):

- HTTP 403 / 404: feed URL changed or access blocked upstream. Check feed publisher status pages.
- Timeout: network connectivity from Databricks to the feed URL. HTTP fetches run on the driver — check Databricks network egress if behind a VPC NAT.
- Parse error: feed format changed. Open `01_bronze_threat_intel_ingest.py` and verify parsing logic against the current raw feed.

### Feed-specific notes

| Feed | URL pattern | Known failure modes |
|------|-------------|---------------------|
| Feodo Tracker | `feodotracker.abuse.ch/downloads/ipblocklist_recommended.txt` | Occasionally returns 503 during updates; retry window is ~1 hour |
| Emerging Threats | `rules.emergingthreats.net/fwrules/emerging-Block-IPs.txt` | Requires no auth; fails if Emerging Threats site is down |
| IPsum | `raw.githubusercontent.com/stamparm/ipsum/master/ipsum.txt` | GitHub raw content; rare outages |

### Recovery

The pipeline is designed for isolated feed failures — one feed failing does not abort the run. If a feed shows 2+ consecutive daily misses:

1. Manually re-run the `bronze_ingest` task from the Databricks job UI.
2. If the task still fails, check the notebook logs for the specific error.
3. If the feed URL has changed, update `FEED_CONFIGS` in `notebooks/threat_intel/00_threat_intel_common.py` and redeploy via Terraform.

---

## 3. VPC Flow Ingest Failures

### Symptoms

- `ingest` task in `bronze-vpc-flow-ingest` job fails
- `bronze.vpc_flow` shows no new rows for > 20 minutes

### Diagnosis

The `ingest` task uses Databricks Auto Loader in `availableNow` trigger mode. Common failure causes:

**No new S3 files (normal, not an error):**
If no EC2 traffic has been generated in the 10-minute window, Auto Loader exits with 0 rows written. The job still succeeds. Confirm by checking S3 for new objects in `vpc-flow-logs/AWSLogs/`.

**S3 access failure:**
Auto Loader reads via the `workload-a-security-logs` and `workload-b-security-logs` external locations. These are backed by the `lakehouse-hub-role` IAM chain.

```bash
# Verify cross-account assume-role chain is functional
aws sts assume-role \
  --role-arn arn:aws:iam::<WORKLOAD_A_ACCOUNT_ID>:role/lakehouse-workload-a-read-only-role \
  --role-session-name test \
  --profile security
```

If this fails, the IAM trust chain may need updating (check if the hub role's external ID or ARN changed since last deploy).

**Checkpoint corruption:**
Auto Loader checkpoints track which S3 files have been processed. A corrupted checkpoint causes Auto Loader to fail at startup or re-process files.

```sql
-- Check the last successful ingest
SELECT
  MAX(_ingested_at) AS last_ingested,
  COUNT(*) AS rows_in_last_5min
FROM security_poc.bronze.vpc_flow
WHERE _ingested_at >= current_timestamp() - INTERVAL 5 MINUTES
```

If `last_ingested` is stale but S3 has files, the checkpoint may need to be reset. Checkpoint paths are stored as `checkpoint_base` in the job parameters (typically `s3://security-lakehouse-managed-<account>/checkpoints/`). Deleting the checkpoint directory and re-running forces a full re-scan of S3 — expect a large first run.

---

## 4. Stale IOCs

### What "stale" means

`silver.threat_intel_network` deletes IOCs when `expires_at < current_timestamp()`. `expires_at = last_seen_at + (2 × TTL)`. If the daily feed ingest fails for multiple consecutive days, `last_seen_at` stops refreshing and IOCs eventually expire.

### Detection

```sql
-- IOCs expiring in the next 48 hours
SELECT
  feed_name,
  COUNT(*) AS expiring_soon,
  MIN(expires_at) AS earliest_expiry
FROM security_poc.silver.threat_intel_network
WHERE expires_at BETWEEN current_timestamp() AND current_timestamp() + INTERVAL 48 HOURS
GROUP BY feed_name
```

```sql
-- Expired IOCs that are still in the silver table (2× TTL hard delete not yet run)
SELECT
  feed_name,
  COUNT(*) AS expired_count,
  MAX(expires_at) AS most_recent_expiry
FROM security_poc.silver.threat_intel_network
WHERE expires_at < current_timestamp()
GROUP BY feed_name
```

### Recovery

Force-run the `threat-intel-pipeline` job to refresh all feeds. If the feed itself is down, IOCs will continue to expire — there is no way to refresh them without the source data.

After an extended outage (> 2 days), some IOCs may have been deleted from silver even though they are still active indicators in the feeds. A fresh run will re-insert them (the `MERGE` key is `ioc_id = SHA-256(ioc_value | feed_name)`, so re-insertion is safe and idempotent).

---

## 5. Alert Forwarding Failures

### Symptoms

- `forward_alerts` task fails
- SNS alerts stop arriving
- `gold.alerts_forwarded` watermark is not advancing

### SNS credential failure

The `forward_alerts` notebook reads AWS credentials from the `security-lakehouse` Databricks Secret Scope. If the IAM access key for `lakehouse-sns-publisher` was rotated or deleted:

1. Check the secret scope: in the Databricks workspace, navigate to **Settings → Secret Scopes** → `security-lakehouse`.
2. Verify that `aws_sns_publisher_access_key_id` and `aws_sns_publisher_secret_access_key` are populated.
3. If keys need rotation, taint the `aws_iam_access_key.sns_publisher` resource in Terraform and re-apply:

```bash
cd environments/poc/
terraform taint module.sns_alerts.aws_iam_access_key.sns_publisher
terraform apply -target=module.sns_alerts.aws_iam_access_key.sns_publisher
```

The new key values are automatically written to Databricks Secrets by the `modules/databricks/jobs` module on the same apply.

### SNS topic policy failure

If the topic policy was changed outside Terraform, the `lakehouse-sns-publisher` IAM user may no longer have `sns:Publish` permission.

```bash
# Test publish directly
aws sns publish \
  --topic-arn arn:aws:sns:us-east-1:<security_account>:security-lakehouse-alerts \
  --message '{"test": "connectivity"}' \
  --subject "Test: pipeline connectivity check" \
  --profile sns_publisher_test
```

If this fails with `AuthorizationError`, re-apply the SNS module to restore the topic policy:

```bash
terraform apply -target=module.sns_alerts.aws_sns_topic_policy.alerts
```

### CDF watermark reset (rare)

If `gold.alerts` was dropped and recreated (e.g., during a schema migration), the CDF version history is lost. On the next `forward_alerts` run:

- `MAX(source_delta_version)` from `gold.alerts_forwarded` references a version that no longer exists in `gold.alerts`.
- The CDF read will fail with a `VersionNotFoundException`.

**Fix:** reset the watermark by finding the current Delta version and inserting a synthetic tracking row:

```sql
-- Find current version
DESCRIBE HISTORY security_poc.gold.alerts LIMIT 1;
-- Note the version number, e.g., 0

-- Insert a sentinel row so the next run reads from version 1 (skipping the full snapshot)
-- Replace '0' with the actual current version
INSERT INTO security_poc.gold.alerts_forwarded
  (alert_id, alert_type, sns_message_id, forwarded_at, source_delta_version)
VALUES
  ('__watermark_reset__', 'ti_network', '__manual__', current_timestamp(), 0);
```

On the next run, `forward_alerts` will read CDF from version 1 forward and process any new inserts cleanly.

---

## 6. Manual Trigger Procedures

### Re-run the daily TI ingest (after a feed failure)

In the Databricks workspace:
1. Navigate to **Workflows → Jobs → threat-intel-pipeline**
2. Click **Run now**
3. Monitor the `bronze_ingest` task output for feed-level success/failure messages

### Re-run the VPC flow ingest + correlation (backfill a gap)

If the `bronze-vpc-flow-ingest` job was paused or failed for an extended period:

1. Navigate to **Workflows → Jobs → bronze-vpc-flow-ingest**
2. Click **Run now**
3. The `gold_alerts` task uses a `lookback_days` parameter (default 30). To backfill alerts for a longer gap, override this at the job task level or run the `02_gold_alerts` notebook directly with `lookback_days = 60`.

### Run `gold_alerts` directly with extended lookback

```python
# In a new Databricks notebook, run the gold_alerts notebook with a custom lookback:
dbutils.notebook.run(
    "/path/to/notebooks/gold/02_gold_alerts",
    timeout_seconds=3600,
    arguments={"lookback_days": "60"}
)
```

### Force-forward all alerts (after a CDF gap)

If `forward_alerts` skipped a batch of alerts and the CDF watermark needs to be manually moved:

1. Delete the forwarding log rows for the affected time range (if no alerts were actually forwarded to SNS):

```sql
-- Only do this if you are certain these alerts were NOT forwarded to SNS
DELETE FROM security_poc.gold.alerts_forwarded
WHERE forwarded_at BETWEEN '<gap_start>' AND '<gap_end>'
  AND sns_message_id != '__manual__';
```

2. Re-run `forward_alerts` — it will re-read from the last valid watermark and republish the missed alerts.

---

## 7. Weekly Reconciliation: Expired Alert Cleanup

The incremental `gold_alerts` design does not automatically remove alerts whose underlying IOC has expired from `silver.threat_intel_network`. A weekly cleanup marks these as `Expired` to keep the alert table accurate.

```sql
-- Mark alerts as Expired when their source IOC no longer exists in silver TI
UPDATE security_poc.gold.alerts a
SET a.status = 'Expired'
WHERE a.alert_type = 'ti_network'
  AND a.status = 'Active'
  AND NOT EXISTS (
    SELECT 1
    FROM security_poc.silver.threat_intel_network ti
    WHERE ti.ioc_id = a.observable_id
  );
```

Run this weekly, or add it as a task to the `threat-intel-pipeline` daily job.

Check how many rows would be affected before running:

```sql
SELECT COUNT(*) AS to_expire
FROM security_poc.gold.alerts a
WHERE a.alert_type = 'ti_network'
  AND a.status = 'Active'
  AND NOT EXISTS (
    SELECT 1
    FROM security_poc.silver.threat_intel_network ti
    WHERE ti.ioc_id = a.observable_id
  );
```

---

## 8. Monitoring Queries Cheat Sheet

```sql
-- Pipeline freshness overview
SELECT
  'bronze.threat_intel_raw' AS tbl,
  CAST(MAX(fetch_timestamp) AS STRING) AS last_write,
  TIMESTAMPDIFF(HOUR, MAX(fetch_timestamp), current_timestamp()) AS hours_ago
FROM security_poc.bronze.threat_intel_raw
UNION ALL
SELECT
  'silver.threat_intel_network' AS tbl,
  CAST(MAX(last_seen_at) AS STRING),
  TIMESTAMPDIFF(HOUR, MAX(last_seen_at), current_timestamp())
FROM security_poc.silver.threat_intel_network
UNION ALL
SELECT
  'bronze.vpc_flow' AS tbl,
  CAST(MAX(_ingested_at) AS STRING),
  TIMESTAMPDIFF(MINUTE, MAX(_ingested_at), current_timestamp())  -- minutes, not hours
FROM security_poc.bronze.vpc_flow
UNION ALL
SELECT
  'gold.alerts_forwarded' AS tbl,
  CAST(MAX(forwarded_at) AS STRING),
  TIMESTAMPDIFF(MINUTE, MAX(forwarded_at), current_timestamp())
FROM security_poc.gold.alerts_forwarded;
```

```sql
-- Active alert volume by severity and source (last 24h)
SELECT
  severity, detection_source, threat_category,
  COUNT(*) AS alert_count,
  SUM(flow_count) AS total_flows
FROM security_poc.gold.alerts
WHERE alert_type = 'ti_network'
  AND status = 'Active'
  AND _computed_at >= current_timestamp() - INTERVAL 24 HOURS
GROUP BY severity, detection_source, threat_category
ORDER BY alert_count DESC;
```

```sql
-- Alerts forwarded in last hour
SELECT
  a.alert_id, a.severity, a.title, a.instance_uid,
  f.forwarded_at, f.sns_message_id
FROM security_poc.gold.alerts_forwarded f
JOIN security_poc.gold.alerts a USING (alert_id)
WHERE f.forwarded_at >= current_timestamp() - INTERVAL 1 HOUR
ORDER BY f.forwarded_at DESC;
```
