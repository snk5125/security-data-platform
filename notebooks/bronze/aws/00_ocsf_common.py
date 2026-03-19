# Databricks notebook source
# -----------------------------------------------------------------------------
# OCSF Common Helpers — Shared by all Bronze OCSF ingestion notebooks
# -----------------------------------------------------------------------------
# Defines constants, struct builders, and mapping functions for the Open
# Cybersecurity Schema Framework (OCSF) v1.1.0. Each bronze notebook uses
# %run ./00_ocsf_common to import these definitions.
#
# OCSF v1.1.0 aligns with Amazon Security Lake source version 2 — the most
# widely adopted stable version with Detection Finding / Compliance Finding
# class separation.
#
# Reference: https://schema.ocsf.io/1.1.0/
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
CLASS_API_ACTIVITY       = 6003   # API calls (CloudTrail default)
CLASS_DETECTION_FINDING  = 2004   # Security findings (GuardDuty)

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
# ACTION — normalized action outcomes (Network Activity)
# =============================================================================
ACTION_UNKNOWN = 0
ACTION_ALLOWED = 1
ACTION_DENIED  = 2

# =============================================================================
# ACTIVITY IDs — per-class action identifiers
# =============================================================================
# API Activity (6003)
ACTIVITY_UNKNOWN = 0
ACTIVITY_CREATE  = 1
ACTIVITY_READ    = 2
ACTIVITY_UPDATE  = 3
ACTIVITY_DELETE   = 4

# Network Activity (4001)
ACTIVITY_TRAFFIC = 6

# Detection Finding (2004)
ACTIVITY_FINDING_CREATE = 1
ACTIVITY_FINDING_UPDATE = 2
ACTIVITY_FINDING_CLOSE  = 3

# COMMAND ----------

# =============================================================================
# CLOUDTRAIL EVENT ROUTING — determines which OCSF class a CloudTrail event
# maps to based on eventName
# =============================================================================

# Console login and MFA events → Authentication (3002)
AUTH_EVENT_NAMES = {
    "ConsoleLogin",
    "CheckMfa",
    "SwitchRole",
    "AssumeRole",
    "AssumeRoleWithSAML",
    "AssumeRoleWithWebIdentity",
    "GetSessionToken",
    "GetFederationToken",
}

# IAM user/role/group/policy mutations → Account Change (3001)
ACCOUNT_CHANGE_EVENT_NAMES = {
    # Users
    "CreateUser", "DeleteUser", "UpdateUser",
    "CreateLoginProfile", "DeleteLoginProfile", "UpdateLoginProfile",
    "EnableMFADevice", "DeactivateMFADevice", "ResyncMFADevice",
    "CreateAccessKey", "DeleteAccessKey", "UpdateAccessKey",
    "CreateServiceSpecificCredential", "DeleteServiceSpecificCredential",
    # Roles
    "CreateRole", "DeleteRole", "UpdateRole",
    "UpdateAssumeRolePolicy",
    "CreateInstanceProfile", "DeleteInstanceProfile",
    "AddRoleToInstanceProfile", "RemoveRoleFromInstanceProfile",
    # Groups
    "CreateGroup", "DeleteGroup", "UpdateGroup",
    "AddUserToGroup", "RemoveUserFromGroup",
    # Policies
    "AttachUserPolicy", "DetachUserPolicy",
    "AttachRolePolicy", "DetachRolePolicy",
    "AttachGroupPolicy", "DetachGroupPolicy",
    "PutUserPolicy", "DeleteUserPolicy",
    "PutRolePolicy", "DeleteRolePolicy",
    "PutGroupPolicy", "DeleteGroupPolicy",
    "CreatePolicy", "DeletePolicy", "CreatePolicyVersion", "DeletePolicyVersion",
    # Service-linked roles
    "CreateServiceLinkedRole", "DeleteServiceLinkedRole",
}

# Everything else → API Activity (6003)

# COMMAND ----------

# =============================================================================
# STRUCT BUILDERS — create OCSF nested objects as Spark struct columns
# =============================================================================

def ocsf_metadata(product_name, log_name=None):
    """
    Build the OCSF metadata struct.
    Required fields: version, product.vendor_name, product.name.
    """
    product_struct = F.struct(
        F.lit("AWS").alias("vendor_name"),
        F.lit(product_name).alias("name"),
    )

    meta = F.struct(
        F.lit(OCSF_VERSION).alias("version"),
        product_struct.alias("product"),
    )

    if log_name:
        meta = F.struct(
            F.lit(OCSF_VERSION).alias("version"),
            product_struct.alias("product"),
            F.lit(log_name).alias("log_name"),
        )

    return meta


def ocsf_cloud(region_col, account_uid_col):
    """
    Build the OCSF cloud struct from column references.
    """
    return F.struct(
        F.lit("AWS").alias("provider"),
        region_col.alias("region"),
        F.struct(
            account_uid_col.cast("string").alias("uid"),
        ).alias("account"),
    )


def compute_type_uid(class_uid_col, activity_id_col):
    """
    OCSF type_uid = class_uid * 100 + activity_id.
    Uniquely identifies the specific event type.
    """
    return (class_uid_col * 100 + activity_id_col).cast("long")


def severity_label(severity_id_col):
    """
    Map severity_id integer to human-readable label string.
    """
    return (
        F.when(severity_id_col == 0, "Unknown")
        .when(severity_id_col == 1, "Informational")
        .when(severity_id_col == 2, "Low")
        .when(severity_id_col == 3, "Medium")
        .when(severity_id_col == 4, "High")
        .when(severity_id_col == 5, "Critical")
        .when(severity_id_col == 6, "Fatal")
        .otherwise("Other")
    )

# COMMAND ----------

# =============================================================================
# GUARDDUTY SEVERITY MAPPING — GuardDuty uses 0-10 float, OCSF uses 0-6 int
# =============================================================================
# Thresholds match Amazon Security Lake's mapping:
#   0       → Unknown (0)
#   0.1-3.9 → Low (2)
#   4.0-6.9 → Medium (3)
#   7.0-8.9 → High (4)
#   9.0-10  → Critical (5)

def guardduty_severity_to_ocsf(severity_col):
    """
    Convert GuardDuty's 0-10 severity float to OCSF severity_id (0-6 integer).
    """
    return (
        F.when(severity_col == 0, F.lit(SEVERITY_UNKNOWN))
        .when(severity_col < 4.0, F.lit(SEVERITY_LOW))
        .when(severity_col < 7.0, F.lit(SEVERITY_MEDIUM))
        .when(severity_col < 9.0, F.lit(SEVERITY_HIGH))
        .otherwise(F.lit(SEVERITY_CRITICAL))
    ).cast("int")

# COMMAND ----------

# =============================================================================
# CLOUDTRAIL ACTIVITY_ID DERIVATION — maps readOnly flag to OCSF activity
# =============================================================================
# CloudTrail's readOnly field:
#   true  → ACTIVITY_READ (2)
#   false → ACTIVITY_CREATE (1) — conservatively; could be update/delete
#   null  → ACTIVITY_UNKNOWN (0)

def cloudtrail_activity_id(read_only_col):
    """
    Derive OCSF activity_id from CloudTrail's readOnly boolean.
    """
    return (
        F.when(read_only_col == True, F.lit(ACTIVITY_READ))
        .when(read_only_col == False, F.lit(ACTIVITY_CREATE))
        .otherwise(F.lit(ACTIVITY_UNKNOWN))
    ).cast("int")

# COMMAND ----------

# =============================================================================
# UTILITY — convert entire raw row to JSON string for raw_data preservation
# =============================================================================

def row_to_raw_json(*cols):
    """
    Serialize all columns of a row into a single JSON string.
    Used to populate the OCSF raw_data field so the original event is preserved.
    """
    return F.to_json(F.struct(*cols))
