# -----------------------------------------------------------------------------
# Bronze Ingestion Jobs Module — Notebooks and Scheduled Jobs
# -----------------------------------------------------------------------------
# Uploads Auto Loader notebooks to the Databricks workspace and creates
# scheduled jobs to run them. Each job ingests from one security data source
# (CloudTrail, VPC Flow Logs, GuardDuty, AWS Config) across both workload
# accounts into bronze Delta tables.
#
# The jobs use serverless compute (environment_key) since the free trial
# workspace is Free Edition and does not support classic clusters.
#
# Resources created: 9 (5 notebooks + 4 jobs)
#
# Prerequisites:
#   - Phase 6: catalog and bronze schema exist
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
  # subdirectory (e.g., /cloudtrail, /vpc_flow).
  checkpoint_base = "s3://${var.managed_storage_bucket_name}/checkpoints/bronze"

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
  path     = "${var.workspace_notebook_path}/00_ocsf_common"
  language = "PYTHON"
  source   = "${var.notebook_source_dir}/00_ocsf_common.py"
}

resource "databricks_notebook" "cloudtrail" {
  depends_on = [databricks_directory.bronze]
  path     = "${var.workspace_notebook_path}/01_bronze_cloudtrail"
  language = "PYTHON"
  source   = "${var.notebook_source_dir}/01_bronze_cloudtrail.py"
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
  name = "bronze-config-ingest"

  task {
    task_key = "ingest"

    notebook_task {
      notebook_path   = databricks_notebook.config.path
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

  # Every 24 hours — Config history changes are infrequent.
  schedule {
    quartz_cron_expression = "0 0 2 * * ?"
    timezone_id            = "UTC"
    pause_status           = "PAUSED"
  }

  tags = {
    phase       = "bronze"
    data_source = "config"
  }
}
