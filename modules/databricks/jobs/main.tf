# -----------------------------------------------------------------------------
# Ingestion Jobs Module — Bronze, Silver & Gold Notebooks and Scheduled Jobs
# -----------------------------------------------------------------------------
# Uploads bronze, silver, and gold notebooks to the Databricks workspace and
# creates scheduled jobs to run them. Bronze jobs ingest from security data
# sources (CloudTrail, VPC Flow Logs, GuardDuty, AWS Config) across both
# workload accounts into bronze Delta tables. The Config job is a multi-task
# workflow that chains bronze ingestion → silver CDC → gold EC2 inventory.
#
# The jobs use serverless compute (environment_key) since the free trial
# workspace is Free Edition and does not support classic clusters.
#
# Resources created: 27 (13 notebooks + 4 directories + 5 jobs + 1 secret scope + 4 secrets)
#
# Prerequisites:
#   - Phase 6: catalog and bronze/silver/gold schemas exist
#   - Phase 7: serverless compute available
#   - S3 data flowing (Phase 4 applied at least 30 minutes ago)
# -----------------------------------------------------------------------------

# ═════════════════════════════════════════════════════════════════════════════
# LOCAL VALUES
# ═════════════════════════════════════════════════════════════════════════════
# Centralizes common parameters passed to all notebook jobs.

locals {
  # Auto Loader checkpoint base path — stored in the managed storage bucket
  # under a dedicated checkpoints prefix. Each notebook appends its own
  # subdirectory (e.g., /cloudtrail, /vpc_flow). Silver notebooks also use
  # this base with a /silver/ subdirectory.
  checkpoint_base = "s3://${var.managed_storage_bucket_name}/checkpoints/bronze"

  # Silver checkpoint uses the same managed bucket but under a separate prefix.
  silver_checkpoint_base = "s3://${var.managed_storage_bucket_name}/checkpoints"

  # Common job parameters passed to every notebook via widgets.
  common_params = {
    workload_a_bucket = var.workload_a_security_logs_bucket_name
    workload_b_bucket = var.workload_b_security_logs_bucket_name
    checkpoint_base   = local.checkpoint_base
  }
}

# ═════════════════════════════════════════════════════════════════════════════
# 1. NOTEBOOKS
# ═════════════════════════════════════════════════════════════════════════════
# Upload each bronze ingestion notebook from the local source directory to
# the Databricks workspace. The PYTHON language is used for PySpark notebooks.
# The parent directory must exist before notebooks can be created in it.

resource "databricks_directory" "bronze" {
  path = var.workspace_notebook_path
}

# Shared OCSF helper notebook — defines constants, struct builders, and mapping
# functions used by all OCSF-formatted bronze notebooks via %run ./00_ocsf_common.
# Must be uploaded before any job that depends on it executes.
resource "databricks_notebook" "ocsf_common" {
  depends_on = [databricks_directory.bronze]
  path       = "${var.workspace_notebook_path}/00_ocsf_common"
  language   = "PYTHON"
  source     = "${var.notebook_source_dir}/00_ocsf_common.py"
}

resource "databricks_notebook" "cloudtrail" {
  depends_on = [databricks_directory.bronze]
  path       = "${var.workspace_notebook_path}/01_bronze_cloudtrail"
  language   = "PYTHON"
  source     = "${var.notebook_source_dir}/01_bronze_cloudtrail.py"
}

resource "databricks_notebook" "vpc_flow" {
  path     = "${var.workspace_notebook_path}/02_bronze_vpc_flow"
  language = "PYTHON"
  source   = "${var.notebook_source_dir}/02_bronze_vpc_flow.py"
}

resource "databricks_notebook" "guardduty" {
  path     = "${var.workspace_notebook_path}/03_bronze_guardduty"
  language = "PYTHON"
  source   = "${var.notebook_source_dir}/03_bronze_guardduty.py"
}

resource "databricks_notebook" "config" {
  path     = "${var.workspace_notebook_path}/04_bronze_config"
  language = "PYTHON"
  source   = "${var.notebook_source_dir}/04_bronze_config.py"
}

# ─────────────────────────────────────────────────────────────────────────────
# Silver notebooks — uploaded to a separate workspace directory.
# Silver notebooks read from bronze Delta tables (not S3 directly), so they
# don't need Auto Loader configuration — just the checkpoint base path.
# ─────────────────────────────────────────────────────────────────────────────

resource "databricks_directory" "silver" {
  path = var.silver_workspace_notebook_path
}

# Config CDC notebook — reads bronze.config_raw, explodes configurationItems,
# and writes a normalized CDC table to silver.config_cdc. Runs as the second
# task in the Config job (after bronze ingestion completes).
resource "databricks_notebook" "config_cdc" {
  depends_on = [databricks_directory.silver]
  path       = "${var.silver_workspace_notebook_path}/01_silver_config_cdc"
  language   = "PYTHON"
  source     = "${var.silver_notebook_source_dir}/01_silver_config_cdc.py"
}

# ─────────────────────────────────────────────────────────────────────────────
# Gold notebooks — uploaded to a separate workspace directory.
# Gold notebooks read from silver Delta tables and produce analytical products
# (current-state views, enriched tables) for downstream consumption.
# ─────────────────────────────────────────────────────────────────────────────

resource "databricks_directory" "gold" {
  path = var.gold_workspace_notebook_path
}

# EC2 inventory notebook — reads silver.config_cdc, joins EC2 instances with
# related resources (ENIs, volumes, security groups, subnets, VPCs), and
# MERGEs a current-state inventory into gold.ec2_inventory. Runs as the third
# task in the Config job (after silver CDC completes).
resource "databricks_notebook" "ec2_inventory" {
  depends_on = [databricks_directory.gold]
  path       = "${var.gold_workspace_notebook_path}/01_gold_ec2_inventory"
  language   = "PYTHON"
  source     = "${var.gold_notebook_source_dir}/01_gold_ec2_inventory.py"
}

# ─────────────────────────────────────────────────────────────────────────────
# Alert forwarding notebook — reads gold.alerts, publishes unforwarded rows
# to AWS SNS using credentials from the Databricks Secret Scope, and appends
# successfully delivered alert IDs to gold.alerts_forwarded.
# Runs as Task 4 of the threat-intel-pipeline job (after gold_alerts).
# ─────────────────────────────────────────────────────────────────────────────

resource "databricks_notebook" "gold_alerts_forward" {
  depends_on = [databricks_directory.gold]
  path       = "${var.gold_workspace_notebook_path}/03_gold_alerts_forward"
  language   = "PYTHON"
  source     = "${var.gold_notebook_source_dir}/03_gold_alerts_forward.py"
}

# ═════════════════════════════════════════════════════════════════════════════
# 1b. DATABRICKS SECRET SCOPE + SECRETS
# ═════════════════════════════════════════════════════════════════════════════
# The "security-lakehouse" secret scope holds AWS credentials for the SNS
# publisher IAM user and the SNS topic ARN. The forwarding notebook reads
# these at runtime via dbutils.secrets.get() — credentials are never stored
# in notebook source or job parameters.
#
# The scope uses the DATABRICKS backend (default). For production, consider
# an AWS Secrets Manager-backed scope to centralise secret rotation and avoid
# storing the raw secret in Terraform state.

resource "databricks_secret_scope" "security_lakehouse" {
  name = "security-lakehouse"
}

# IAM access key ID for the SNS publisher user.
resource "databricks_secret" "sns_access_key_id" {
  scope        = databricks_secret_scope.security_lakehouse.name
  key          = "aws_sns_publisher_access_key_id"
  string_value = var.sns_publisher_access_key_id
}

# IAM access key secret for the SNS publisher user.
resource "databricks_secret" "sns_secret_access_key" {
  scope        = databricks_secret_scope.security_lakehouse.name
  key          = "aws_sns_publisher_secret_access_key"
  string_value = var.sns_publisher_secret_access_key
}

# SNS topic ARN — the publish destination.
resource "databricks_secret" "sns_topic_arn" {
  scope        = databricks_secret_scope.security_lakehouse.name
  key          = "aws_sns_topic_arn"
  string_value = var.sns_topic_arn
}

# AWS region where the SNS topic lives.
# Stored as a secret for consistency; not sensitive, but collocating all
# SNS-related config in one scope simplifies notebook code and rotation.
resource "databricks_secret" "aws_region" {
  scope        = databricks_secret_scope.security_lakehouse.name
  key          = "aws_region"
  string_value = var.aws_region
}

# ═════════════════════════════════════════════════════════════════════════════
# 2. JOBS
# ═════════════════════════════════════════════════════════════════════════════
# Each job runs one notebook on serverless compute. Jobs use the
# "environment" block for serverless Python execution. The schedule triggers
# periodic ingestion — frequencies are tuned per data source:
#   - CloudTrail: every 15 min (frequent management events)
#   - VPC Flow Logs: every 10 min (continuous network flows)
#   - GuardDuty: every 6 hours (findings arrive in batches)
#   - Config: every 24 hours (history changes are low-frequency)
#
# Jobs are paused by default (pause_status = "PAUSED") so they don't run
# until explicitly unpaused after validation.

resource "databricks_job" "cloudtrail" {
  # Depends on the OCSF common notebook because the CloudTrail notebook uses
  # %run ./00_ocsf_common to import shared OCSF helpers.
  depends_on = [databricks_notebook.ocsf_common]
  name       = "bronze-cloudtrail-ingest"

  task {
    task_key = "ingest"

    notebook_task {
      notebook_path   = databricks_notebook.cloudtrail.path
      base_parameters = local.common_params
    }

    # Serverless compute — uses the default environment for Python notebooks.
    environment_key = "Default"
  }

  environment {
    environment_key = "Default"

    spec {
      client = "1"
    }
  }

  # Every 15 minutes — CloudTrail delivers events with ~5 min latency.
  schedule {
    quartz_cron_expression = "0 0/15 * * * ?"
    timezone_id            = "UTC"
    pause_status           = "PAUSED"
  }

  tags = {
    phase       = "bronze"
    data_source = "cloudtrail"
  }
}

resource "databricks_job" "vpc_flow" {
  # This job runs every 10 minutes — aligned to the VPC Flow Log aggregation
  # window. It chains three tasks:
  #   1. ingest        — Auto Loader reads new flow files from S3 → bronze.vpc_flow
  #   2. gold_alerts   — incremental watermark correlation (bronze.vpc_flow × silver.threat_intel_network)
  #   3. forward_alerts — CDF-based SNS publish of newly inserted gold.alerts rows
  #
  # Rationale for combining these tasks:
  #   VPC Flow data arrives every 10 minutes. Running gold_alerts as a dependent
  #   task after ingest means each correlation run processes only the ~10 minutes
  #   of new flow data written in this cycle (watermark-bounded), not a full
  #   30-day recompute. This reduces alert latency from up to 24 hours to ~10–15
  #   minutes while keeping per-run compute proportional to data volume.
  #
  # Secrets must exist before the job is created so the forwarding notebook can
  # immediately resolve dbutils.secrets.get() when task 3 runs.
  depends_on = [
    databricks_notebook.ocsf_common,
    databricks_notebook.gold_alerts,
    databricks_notebook.gold_alerts_forward,
    databricks_secret.sns_access_key_id,
    databricks_secret.sns_secret_access_key,
    databricks_secret.sns_topic_arn,
    databricks_secret.aws_region,
  ]
  name = "bronze-vpc-flow-ingest"

  # Task 1: Bronze ingest — Auto Loader reads new VPC Flow log files from S3
  # and appends OCSF-formatted rows to bronze.vpc_flow. Uses a file-level
  # checkpoint so each file is processed exactly once across runs.
  task {
    task_key = "ingest"

    notebook_task {
      notebook_path   = databricks_notebook.vpc_flow.path
      base_parameters = local.common_params
    }

    environment_key = "Default"
  }

  # Task 2: Gold alerts — incremental watermark correlation.
  # Reads only VPC Flow rows with _ingested_at > MAX(_ingested_at) already
  # in gold.alerts, joins against the full active silver.threat_intel_network,
  # and MERGEs results into gold.alerts. Cost is O(10 min of new flows), not
  # O(30 days) — runs after ingest so it always processes the freshest data.
  task {
    task_key = "gold_alerts"

    depends_on {
      task_key = "ingest"
    }

    notebook_task {
      notebook_path   = databricks_notebook.gold_alerts.path
      base_parameters = {
        # bootstrap_lookback_days controls the lookback window used only on the
        # very first run when gold.alerts is empty. Subsequent runs use the
        # watermark and ignore this parameter entirely.
        bootstrap_lookback_days = "1"
      }
    }

    environment_key = "Default"
  }

  # Task 3: Alert forwarding — CDF-based SNS publish.
  # Reads only the rows written to gold.alerts since the last processed Delta
  # version (source_delta_version high-water mark in alerts_forwarded), filters
  # to _change_type = "insert", applies an anti-join safety net, and publishes
  # each new alert to SNS. Only runs after gold_alerts succeeds so it always
  # forwards the output of the current correlation run.
  task {
    task_key = "forward_alerts"

    depends_on {
      task_key = "gold_alerts"
    }

    notebook_task {
      notebook_path   = databricks_notebook.gold_alerts_forward.path
      base_parameters = {}
    }

    environment_key = "Default"
  }

  environment {
    environment_key = "Default"

    spec {
      client = "1"
    }
  }

  # Every 10 minutes — VPC Flow Logs aggregate in 10-min windows.
  schedule {
    quartz_cron_expression = "0 0/10 * * * ?"
    timezone_id            = "UTC"
    pause_status           = "PAUSED"
  }

  tags = {
    phase       = "bronze-gold-forward"
    data_source = "vpc_flow"
  }
}

resource "databricks_job" "guardduty" {
  depends_on = [databricks_notebook.ocsf_common]
  name       = "bronze-guardduty-ingest"

  task {
    task_key = "ingest"

    notebook_task {
      notebook_path   = databricks_notebook.guardduty.path
      base_parameters = local.common_params
    }

    environment_key = "Default"
  }

  environment {
    environment_key = "Default"

    spec {
      client = "1"
    }
  }

  # Every 6 hours — GuardDuty exports findings in ~30 min batches.
  schedule {
    quartz_cron_expression = "0 0 0/6 * * ?"
    timezone_id            = "UTC"
    pause_status           = "PAUSED"
  }

  tags = {
    phase       = "bronze"
    data_source = "guardduty"
  }
}

resource "databricks_job" "config" {
  # Multi-task workflow: bronze ingestion → silver CDC → gold EC2 inventory.
  # Each task depends on the previous, guaranteeing the full pipeline runs
  # in order without separate jobs or schedule coordination.
  name = "config-pipeline"

  # Task 1: Bronze ingestion — reads raw Config JSON from S3 into
  # bronze.config_raw using Auto Loader.
  task {
    task_key = "bronze_ingest"

    notebook_task {
      notebook_path   = databricks_notebook.config.path
      base_parameters = local.common_params
    }

    environment_key = "Default"
  }

  # Task 2: Silver CDC — reads new rows from bronze.config_raw, explodes
  # configurationItems, and appends normalized CDC rows to silver.config_cdc.
  # Only runs after bronze_ingest succeeds.
  task {
    task_key = "silver_cdc"

    depends_on {
      task_key = "bronze_ingest"
    }

    notebook_task {
      notebook_path = databricks_notebook.config_cdc.path
      base_parameters = {
        checkpoint_base = local.silver_checkpoint_base
      }
    }

    environment_key = "Default"
  }

  # Task 3: Gold EC2 inventory — reads silver.config_cdc, joins EC2 instances
  # with related resources (ENIs, volumes, SGs, subnets, VPCs), and MERGEs
  # a current-state inventory into gold.ec2_inventory. Only runs after
  # silver_cdc succeeds so it always sees the latest CDC data.
  task {
    task_key = "gold_ec2_inventory"

    depends_on {
      task_key = "silver_cdc"
    }

    notebook_task {
      notebook_path   = databricks_notebook.ec2_inventory.path
      base_parameters = {}
    }

    environment_key = "Default"
  }

  environment {
    environment_key = "Default"

    spec {
      client = "1"
    }
  }

  # Every 24 hours — Config history changes are infrequent.
  schedule {
    quartz_cron_expression = "0 0 2 * * ?"
    timezone_id            = "UTC"
    pause_status           = "PAUSED"
  }

  tags = {
    phase       = "bronze-silver-gold"
    data_source = "config"
  }
}

# ─────────────────────────────────────────────────────────────────────────────
# Threat intel notebooks — uploaded to a dedicated workspace directory.
# These notebooks do not use Auto Loader (no S3 source) — they fetch from
# public HTTP endpoints and write to bronze/silver Delta tables directly.
# ─────────────────────────────────────────────────────────────────────────────

resource "databricks_directory" "threat_intel" {
  path = var.threat_intel_workspace_notebook_path
}

# Shared threat intel helper notebook — defines FEED_CONFIGS, table name
# constants, BRONZE_TI_SCHEMA, and IP normalization UDFs (parse_network_ioc,
# is_private_network, ip_in_network). Loaded via %run by the pipeline notebooks.
# Must be uploaded before any task that depends on it executes.
resource "databricks_notebook" "threat_intel_common" {
  depends_on = [databricks_directory.threat_intel]
  path       = "${var.threat_intel_workspace_notebook_path}/00_threat_intel_common"
  language   = "PYTHON"
  source     = "${var.threat_intel_notebook_source_dir}/00_threat_intel_common.py"
}

# Bronze ingest notebook — fetches the 3 configured threat intel feeds via
# HTTP GET, parses each format, and appends IOC rows to bronze.threat_intel_raw.
# Handles per-feed failures gracefully (skips failed feeds, continues run).
# Runs as Task 1 of the threat-intel-pipeline job.
resource "databricks_notebook" "threat_intel_bronze_ingest" {
  depends_on = [databricks_directory.threat_intel]
  path       = "${var.threat_intel_workspace_notebook_path}/01_bronze_threat_intel_ingest"
  language   = "PYTHON"
  source     = "${var.threat_intel_notebook_source_dir}/01_bronze_threat_intel_ingest.py"
}

# Silver network notebook — normalizes IP/CIDR indicators, filters RFC 1918,
# MERGEs into silver.threat_intel_network with TTL lifecycle management, and
# hard-deletes expired indicators (last_seen_at older than 2× TTL).
# Also creates placeholder silver tables for dns and hash indicator types.
# Runs as Task 2 of the threat-intel-pipeline job (after bronze_ingest).
resource "databricks_notebook" "threat_intel_silver_network" {
  depends_on = [databricks_directory.threat_intel]
  path       = "${var.threat_intel_workspace_notebook_path}/02_silver_threat_intel_network"
  language   = "PYTHON"
  source     = "${var.threat_intel_notebook_source_dir}/02_silver_threat_intel_network.py"
}

# ─────────────────────────────────────────────────────────────────────────────
# Gold notebooks — uploaded to the shared gold workspace directory.
# The gold alerts notebook reads from bronze.vpc_flow and
# silver.threat_intel_network to produce the unified gold.alerts table.
#
# Runs as Task 2 of the bronze-vpc-flow-ingest job (NOT the threat-intel-pipeline).
# Correlation is triggered by each 10-minute VPC Flow ingest cycle so that
# alert latency is ~10 minutes rather than up to 24 hours.
# ─────────────────────────────────────────────────────────────────────────────

# Unified alerts notebook — correlates VPC Flow logs against active threat intel
# indicators (exact-match and CIDR), aggregates hits per instance/IOC, and
# MERGEs results into gold.alerts using an incremental watermark strategy.
# The table schema is designed to accommodate future alert types (av_detection,
# ti_hash, ti_dns) without schema changes.
resource "databricks_notebook" "gold_alerts" {
  depends_on = [databricks_directory.gold]
  path       = "${var.gold_workspace_notebook_path}/02_gold_alerts"
  language   = "PYTHON"
  source     = "${var.gold_notebook_source_dir}/02_gold_alerts.py"
}

# ═════════════════════════════════════════════════════════════════════════════
# THREAT INTEL PIPELINE JOB
# ═════════════════════════════════════════════════════════════════════════════
# Two-task workflow: bronze_ingest → silver_network.
#
# Why only two tasks (not four):
#   The original design had four tasks: bronze_ingest → silver_network →
#   gold_alerts → forward_alerts. This meant correlation only ran once per day.
#   A VPC Flow record written at 01:01 UTC would not produce an alert until
#   the following day — up to 24 hours of latency.
#
#   Gold correlation and alert forwarding have been moved to the
#   bronze-vpc-flow-ingest job (every 10 minutes) as Tasks 2 and 3.
#   This aligns each stage with its data source's natural update cadence:
#     - TI feeds update once per day → threat-intel-pipeline stays daily
#     - VPC Flow arrives every 10 min → correlation runs every 10 min
#
#   The silver.threat_intel_network table is fully refreshed before the next
#   VPC Flow ingest cycle starts, so the incremental gold_alerts correlation
#   always sees a current indicator set.
#
# Schedule: daily at 01:00 UTC (runs before the Config pipeline at 02:00 UTC
# and well before the VPC Flow job cycles, so indicators are current before
# any correlation runs use them).

resource "databricks_job" "threat_intel_pipeline" {
  # Only the two TI pipeline notebooks are needed — gold/forward notebooks
  # are referenced by the vpc_flow job instead.
  depends_on = [
    databricks_notebook.threat_intel_common,
    databricks_notebook.threat_intel_bronze_ingest,
    databricks_notebook.threat_intel_silver_network,
  ]
  name = "threat-intel-pipeline"

  # Task 1: Bronze ingest — fetch 3 feeds (Feodo Tracker, Emerging Threats,
  # IPsum), parse each format, append IOC rows to bronze.threat_intel_raw,
  # and hard-delete bronze rows older than 14 days.
  task {
    task_key = "bronze_ingest"

    notebook_task {
      notebook_path   = databricks_notebook.threat_intel_bronze_ingest.path
      base_parameters = {}
    }

    environment_key = "Default"
  }

  # Task 2: Silver normalization — reads the latest bronze batch, normalizes
  # IPs and CIDRs, filters RFC 1918 private addresses, MERGEs into
  # silver.threat_intel_network, refreshes last_seen_at for existing indicators,
  # and hard-deletes expired indicators (last_seen_at older than 2× TTL).
  # Only runs after bronze_ingest succeeds — guarantees silver always reflects
  # the current day's fetch before correlation jobs use the table.
  task {
    task_key = "silver_network"

    depends_on {
      task_key = "bronze_ingest"
    }

    notebook_task {
      notebook_path   = databricks_notebook.threat_intel_silver_network.path
      base_parameters = {}
    }

    environment_key = "Default"
  }

  environment {
    environment_key = "Default"

    spec {
      client = "1"
    }
  }

  # Daily at 01:00 UTC — feeds publish once per day; running at 01:00 ensures
  # silver.threat_intel_network is fully refreshed before the VPC Flow job
  # cycles begin at the top of the next 10-minute window. The Config pipeline
  # runs at 02:00 UTC, so TI refresh completes well before it starts.
  schedule {
    quartz_cron_expression = "0 0 1 * * ?"
    timezone_id            = "UTC"
    pause_status           = "PAUSED"
  }

  tags = {
    phase       = "bronze-silver"
    data_source = "threat_intel"
  }
}
