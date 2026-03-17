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
# Resources created: 14 (7 notebooks + 3 directories + 4 jobs)
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
  depends_on = [databricks_notebook.ocsf_common]
  name       = "bronze-vpc-flow-ingest"

  task {
    task_key = "ingest"

    notebook_task {
      notebook_path   = databricks_notebook.vpc_flow.path
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

  # Every 10 minutes — VPC Flow Logs aggregate in 10-min windows.
  schedule {
    quartz_cron_expression = "0 0/10 * * * ?"
    timezone_id            = "UTC"
    pause_status           = "PAUSED"
  }

  tags = {
    phase       = "bronze"
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
