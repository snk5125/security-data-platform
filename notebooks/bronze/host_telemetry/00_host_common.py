# Databricks notebook source
# -----------------------------------------------------------------------------
# OCSF Common Helpers for Host Telemetry — Shared by all Host Bronze notebooks
# -----------------------------------------------------------------------------
# Defines constants, struct builders, and mapping functions for the Open
# Cybersecurity Schema Framework (OCSF) v1.1.0, specialized for host-level
# telemetry data sources (bash_history, auth logs, syslog, Windows Events,
# auditd). Each host bronze notebook uses %run ./00_host_common to import
# these definitions.
#
# This file is intentionally self-contained and does NOT import from the AWS,
# Azure, or GCP common helpers. The OCSF constants are identical across clouds
# (they are spec-level values), but keeping each source's helpers independent
# avoids cross-cloud coupling and allows host-specific utilities to live
# alongside the shared OCSF bits.
#
# Host telemetry is collected by Cribl Edge agents running on EC2/Azure/GCP
# instances and forwarded to cloud storage in JSON format, partitioned by
# source_type (e.g., source_type=bash_history/, source_type=auth/, etc.).
#
# OCSF v1.1.0 reference: https://schema.ocsf.io/1.1.0/
# -----------------------------------------------------------------------------

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.types import (
    StructType, StructField, StringType, IntegerType, LongType,
    TimestampType, MapType, ArrayType, FloatType
)

# COMMAND ----------

# =============================================================================
# OCSF VERSION
# =============================================================================
OCSF_VERSION = "1.1.0"

# =============================================================================
# CATEGORY UIDs — top-level grouping of event classes
# =============================================================================
CATEGORY_SYSTEM       = 1   # System Activity
CATEGORY_FINDINGS     = 2   # Findings
CATEGORY_IAM          = 3   # Identity & Access Management
CATEGORY_NETWORK      = 4   # Network Activity
CATEGORY_DISCOVERY    = 5   # Discovery
CATEGORY_APPLICATION  = 6   # Application Activity

# =============================================================================
# CLASS UIDs — specific event types within categories
# =============================================================================
CLASS_ACCOUNT_CHANGE     = 3001   # IAM account/user/role mutations
CLASS_AUTHENTICATION     = 3002   # Console login, MFA, federation
CLASS_NETWORK_ACTIVITY   = 4001   # Network traffic (VPC Flow Logs)
CLASS_API_ACTIVITY       = 6003   # API calls, command execution

# =============================================================================
# SEVERITY — normalized severity scale (same across all OCSF classes)
# =============================================================================
SEVERITY_UNKNOWN       = 0
SEVERITY_INFORMATIONAL = 1
SEVERITY_LOW           = 2
SEVERITY_MEDIUM        = 3
SEVERITY_HIGH          = 4
SEVERITY_CRITICAL      = 5
SEVERITY_FATAL         = 6

SEVERITY_LABELS = {
    0: "Unknown",
    1: "Informational",
    2: "Low",
    3: "Medium",
    4: "High",
    5: "Critical",
    6: "Fatal",
}

# =============================================================================
# STATUS — normalized event status
# =============================================================================
STATUS_UNKNOWN = 0
STATUS_SUCCESS = 1
STATUS_FAILURE = 2

# =============================================================================
# ACTIVITY IDs — per-class action identifiers
# =============================================================================
# API Activity (6003)
ACTIVITY_UNKNOWN = 0
ACTIVITY_CREATE  = 1
ACTIVITY_READ    = 2
ACTIVITY_UPDATE  = 3
ACTIVITY_DELETE  = 4

# =============================================================================
# PRODUCT IDENTITY — Cribl Edge is the collection agent for host telemetry
# =============================================================================
PRODUCT_NAME   = "cribl-edge"
PRODUCT_VENDOR = "Cribl"

# COMMAND ----------

# =============================================================================
# HOST OCSF SCHEMA — defines the target Delta table structure for all host
# telemetry bronze tables. All host notebooks produce rows conforming to this
# schema. The schema uses OCSF v1.1.0 field names and nesting conventions.
# =============================================================================

HOST_OCSF_SCHEMA = StructType([
    # ── Time — when the event occurred ──
    StructField("time", TimestampType(), True),

    # ── Classification ──
    StructField("class_uid", IntegerType(), True),
    StructField("category_uid", IntegerType(), True),
    StructField("severity_id", IntegerType(), True),
    StructField("status_id", IntegerType(), True),
    StructField("activity_id", IntegerType(), True),

    # ── Message — human-readable summary ──
    StructField("message", StringType(), True),

    # ── Actor — who performed the action ──
    StructField("actor", StructType([
        StructField("user", StructType([
            StructField("name", StringType(), True),
            StructField("uid", StringType(), True),
        ]), True),
    ]), True),

    # ── Device — the host where the event occurred ──
    StructField("device", StructType([
        StructField("hostname", StringType(), True),
        StructField("ip", StringType(), True),
        StructField("os", StructType([
            StructField("name", StringType(), True),
            StructField("type", StringType(), True),
        ]), True),
    ]), True),

    # ── Metadata — product and processing info ──
    StructField("metadata", StructType([
        StructField("product", StructType([
            StructField("name", StringType(), True),
            StructField("vendor_name", StringType(), True),
        ]), True),
        StructField("version", StringType(), True),
        StructField("labels", MapType(StringType(), StringType()), True),
    ]), True),

    # ── Source URL — storage path the event was read from ──
    StructField("src_url", StringType(), True),

    # ── Raw event — original event preserved as string ──
    StructField("raw_event", StringType(), True),
])

# COMMAND ----------

# =============================================================================
# STRUCT BUILDERS — create OCSF nested objects as Spark struct columns
# =============================================================================

def build_ocsf_metadata(source_type, obfuscated=False):
    """
    Build the OCSF metadata struct for host telemetry data sources.

    Args:
        source_type: The Cribl source type label (e.g., "bash_history", "auth",
                     "syslog", "windows_security", "auditd").
        obfuscated: If True, adds an "obfuscated" label to indicate the raw
                    event data has been sanitized (e.g., bash_history commands
                    may have sensitive arguments masked by Cribl).

    Returns a Spark struct column with product, version, and labels fields.
    """
    labels = {"source_type": source_type}
    if obfuscated:
        labels["obfuscated"] = "true"

    # Build the labels map as a Spark literal
    labels_col = F.create_map(
        *[item for k, v in labels.items() for item in (F.lit(k), F.lit(v))]
    )

    return F.struct(
        F.struct(
            F.lit(PRODUCT_NAME).alias("name"),
            F.lit(PRODUCT_VENDOR).alias("vendor_name"),
        ).alias("product"),
        F.lit(OCSF_VERSION).alias("version"),
        labels_col.alias("labels"),
    )


def build_actor_struct(user_col, uid_col=None):
    """
    Build the OCSF actor struct from column name strings.

    Args:
        user_col: Name of the column containing the username.
        uid_col:  Name of the column containing the user ID (optional).
                  If None, uid is set to null.

    Returns a Spark struct column with actor.user.name and actor.user.uid.
    """
    user_uid = F.col(uid_col) if uid_col else F.lit(None).cast("string")

    return F.struct(
        F.struct(
            F.col(user_col).alias("name"),
            user_uid.alias("uid"),
        ).alias("user"),
    )


def build_device_struct(hostname_col, ip_col=None, os_name="Linux", os_type="Linux"):
    """
    Build the OCSF device struct from column name strings.

    Args:
        hostname_col: Name of the column containing the hostname.
        ip_col:       Name of the column containing the IP address (optional).
                      If None, ip is set to null.
        os_name:      Operating system name string (default: "Linux").
        os_type:      Operating system type string (default: "Linux").

    Returns a Spark struct column with device.hostname, device.ip, and device.os.
    """
    ip_value = F.col(ip_col) if ip_col else F.lit(None).cast("string")

    return F.struct(
        F.col(hostname_col).alias("hostname"),
        ip_value.alias("ip"),
        F.struct(
            F.lit(os_name).alias("name"),
            F.lit(os_type).alias("type"),
        ).alias("os"),
    )

# COMMAND ----------

# =============================================================================
# UTILITY — run OPTIMIZE + ZORDER on a Delta table, catching "not supported"
# errors gracefully. Serverless compute and some warehouse tiers do not support
# OPTIMIZE, so we swallow the error rather than failing the notebook.
# =============================================================================

def try_optimize(spark, table_name, zorder_cols):
    """
    Attempt to run OPTIMIZE with ZORDER on the given Delta table.

    Args:
        spark:        The SparkSession.
        table_name:   Fully qualified Delta table name (e.g., "catalog.schema.table").
        zorder_cols:  List of column name strings to ZORDER by.

    If the runtime does not support OPTIMIZE (e.g., serverless Starter
    Warehouse), the error is caught and logged. The notebook continues
    without failing.
    """
    zorder_clause = ", ".join(zorder_cols)
    sql = f"OPTIMIZE {table_name} ZORDER BY ({zorder_clause})"
    try:
        spark.sql(sql)
        print(f"OPTIMIZE complete: {table_name} (ZORDER BY {zorder_clause})")
    except Exception as e:
        err = str(e).lower()
        if "not supported" in err or "optimize" in err or "syntax" in err:
            print(f"OPTIMIZE skipped (not supported on this compute): {table_name}")
        else:
            raise

# COMMAND ----------

# =============================================================================
# UTILITY — convert entire raw row to JSON string for raw_event preservation
# =============================================================================

def row_to_raw_json(*cols):
    """
    Serialize all columns of a row into a single JSON string.
    Used to populate the OCSF raw_event field so the original event is preserved.
    """
    return F.to_json(F.struct(*cols))
