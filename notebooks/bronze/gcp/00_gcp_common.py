# Databricks notebook source
# -----------------------------------------------------------------------------
# OCSF Common Helpers for GCP — Shared by all GCP Bronze ingestion notebooks
# -----------------------------------------------------------------------------
# Defines constants, struct builders, and mapping functions for the Open
# Cybersecurity Schema Framework (OCSF) v1.1.0, specialized for GCP data
# sources. Each GCP bronze notebook uses %run ./00_gcp_common to import
# these definitions.
#
# This file is intentionally self-contained and does NOT import from the AWS
# or Azure common helpers. The OCSF constants are identical across clouds
# (they are spec-level values), but keeping each cloud's helpers independent
# avoids cross-cloud coupling and allows cloud-specific utilities (e.g., GCP
# resource name parsing) to live alongside the shared OCSF bits.
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
CLASS_AUTHENTICATION     = 3002   # Sign-in, OAuth, federation
CLASS_NETWORK_ACTIVITY   = 4001   # Network traffic (VPC Flow Logs)
CLASS_SECURITY_FINDING   = 2001   # Security findings (SCC)
CLASS_API_ACTIVITY       = 6003   # API / resource operations (Cloud Audit Logs)
CLASS_DETECTION_FINDING  = 2004   # Detection findings (for reference)

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

# Security Finding (2001) / Detection Finding (2004)
ACTIVITY_FINDING_CREATE = 1
ACTIVITY_FINDING_UPDATE = 2
ACTIVITY_FINDING_CLOSE  = 3

# COMMAND ----------

# =============================================================================
# CLOUD AUDIT LOG EVENT ROUTING — determines which OCSF class an audit log
# event maps to based on protoPayload.methodName
# =============================================================================

# Authentication and token events -> Authentication (3002)
# These methodNames indicate sign-in, OAuth token, or session creation events.
AUTH_METHOD_PATTERNS = [
    "LoginProfile",                         # Console login profile operations
    "google.login.LoginService",            # Workspace/Cloud Identity login events
    "google.cloud.identitytoolkit",         # Identity Platform (Firebase Auth)
    "GenerateAccessToken",                  # IAM service account token generation
    "GenerateIdToken",                      # IAM service account ID token generation
    "SignJwt",                              # IAM JWT signing (federated auth)
    "SignBlob",                             # IAM blob signing
]

# IAM mutations -> Account Change (3001)
# These methodNames indicate IAM policy, role, or service account changes.
ACCOUNT_CHANGE_METHOD_PATTERNS = [
    "SetIamPolicy",                         # Set IAM policy on any resource
    "google.iam.admin.v1.CreateRole",       # Custom role creation
    "google.iam.admin.v1.UpdateRole",       # Custom role update
    "google.iam.admin.v1.DeleteRole",       # Custom role deletion
    "google.iam.admin.v1.UndeleteRole",     # Custom role restore
    "CreateServiceAccount",                 # Service account creation
    "DeleteServiceAccount",                 # Service account deletion
    "UpdateServiceAccount",                 # Service account update
    "DisableServiceAccount",                # Service account disable
    "EnableServiceAccount",                 # Service account enable
    "CreateServiceAccountKey",              # SA key creation
    "DeleteServiceAccountKey",              # SA key deletion
    "UploadServiceAccountKey",              # SA key upload
]

# Everything else -> API Activity (6003) — default for GCP resource operations

# COMMAND ----------

# =============================================================================
# STRUCT BUILDERS — create OCSF nested objects as Spark struct columns
# =============================================================================

def ocsf_metadata(product_name, log_name=None):
    """
    Build the OCSF metadata struct for GCP data sources.
    Required fields: version, product.vendor_name, product.name.
    """
    product_struct = F.struct(
        F.lit("GCP").alias("vendor_name"),
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


def ocsf_cloud(region_col, project_uid_col):
    """
    Build the OCSF cloud struct from column references.
    GCP uses project IDs where AWS uses account IDs and Azure uses subscription
    IDs. All map to cloud.account.uid in OCSF.
    """
    return F.struct(
        F.lit("GCP").alias("provider"),
        region_col.alias("region"),
        F.struct(
            project_uid_col.cast("string").alias("uid"),
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
# GCP RESOURCE NAME PARSER — extracts structured components from GCP resource
# names of the form:
#   //compute.googleapis.com/projects/{project}/zones/{zone}/instances/{name}
#   //storage.googleapis.com/projects/_/buckets/{name}
#   //iam.googleapis.com/projects/{project}/serviceAccounts/{email}
# =============================================================================
# GCP resource names use a hierarchical format starting with a double-slash
# service prefix. This function extracts the key components into a Spark struct
# for use in OCSF cloud.resource fields.

def parse_gcp_resource_name(resource_name_col):
    """
    Parse a GCP resource name column into a struct with:
      - service:        the API service (e.g., compute.googleapis.com)
      - project_id:     the GCP project ID
      - resource_type:  inferred resource type from the path (e.g., instances, buckets)
      - resource_name:  the resource name (last path segment)
      - full_name:      the original resource name (preserved for unmapped use)

    Handles the common GCP resource name format:
      //{service}/projects/{project}/{location_type}/{location}/{type}/{name}
    """
    # Strip the leading "//" and split on "/"
    # e.g., "//compute.googleapis.com/projects/my-proj/zones/us-central1-a/instances/vm-1"
    # After stripping "//": "compute.googleapis.com/projects/my-proj/zones/us-central1-a/instances/vm-1"
    stripped = F.regexp_replace(resource_name_col, "^//", "")
    parts = F.split(stripped, "/")

    return F.struct(
        # Service is the first segment (e.g., "compute.googleapis.com")
        parts.getItem(0).alias("service"),
        # Project ID is typically at index 2 (after "projects")
        # Some resource names use "_" as project placeholder (e.g., storage)
        parts.getItem(2).alias("project_id"),
        # Resource type is the second-to-last segment
        # (e.g., "instances", "buckets", "serviceAccounts")
        F.element_at(parts, -2).alias("resource_type"),
        # Resource name is the last segment
        F.element_at(parts, -1).alias("resource_name"),
        # Preserve the full resource name for unmapped / forensic use
        resource_name_col.alias("full_name"),
    )

# COMMAND ----------

# =============================================================================
# CLOUD AUDIT LOG HELPERS — derive OCSF activity_id from method name
# =============================================================================
# GCP Cloud Audit Log protoPayload.methodName follows patterns like:
#   google.cloud.compute.v1.Instances.Insert
#   google.iam.admin.v1.CreateRole
#   storage.objects.get
# We map common verbs to OCSF activity IDs.

def gcp_activity_id(method_name_col):
    """
    Derive OCSF activity_id from GCP Cloud Audit Log methodName.
    The last segment of the methodName typically indicates the verb:
      Insert/Create  -> CREATE (1)
      Get/List       -> READ (2)
      Update/Patch/Set -> UPDATE (3)
      Delete/Remove  -> DELETE (4)
      other          -> UNKNOWN (0)
    """
    # Extract the last segment (the verb) from the methodName.
    # GCP methodNames use dots and sometimes mixed formats.
    verb = F.lower(F.element_at(F.split(method_name_col, "\\."), -1))

    return (
        F.when(verb.contains("insert"), F.lit(ACTIVITY_CREATE))
        .when(verb.contains("create"), F.lit(ACTIVITY_CREATE))
        .when(verb.contains("get"), F.lit(ACTIVITY_READ))
        .when(verb.contains("list"), F.lit(ACTIVITY_READ))
        .when(verb.contains("update"), F.lit(ACTIVITY_UPDATE))
        .when(verb.contains("patch"), F.lit(ACTIVITY_UPDATE))
        .when(verb.contains("set"), F.lit(ACTIVITY_UPDATE))
        .when(verb.contains("delete"), F.lit(ACTIVITY_DELETE))
        .when(verb.contains("remove"), F.lit(ACTIVITY_DELETE))
        .otherwise(F.lit(ACTIVITY_UNKNOWN))
    ).cast("int")

# COMMAND ----------

# =============================================================================
# GCP SEVERITY MAPPING — Cloud Logging severity to OCSF severity_id
# =============================================================================
# Cloud Logging uses string severity values: DEFAULT, DEBUG, INFO, NOTICE,
# WARNING, ERROR, CRITICAL, ALERT, EMERGENCY. Map these to OCSF severity IDs.

def gcp_severity_to_ocsf(severity_col):
    """
    Convert GCP Cloud Logging severity string to OCSF severity_id.
    """
    upper_sev = F.upper(severity_col)
    return (
        F.when(upper_sev == "DEFAULT", F.lit(SEVERITY_UNKNOWN))
        .when(upper_sev == "DEBUG", F.lit(SEVERITY_INFORMATIONAL))
        .when(upper_sev == "INFO", F.lit(SEVERITY_INFORMATIONAL))
        .when(upper_sev == "NOTICE", F.lit(SEVERITY_INFORMATIONAL))
        .when(upper_sev == "WARNING", F.lit(SEVERITY_MEDIUM))
        .when(upper_sev == "ERROR", F.lit(SEVERITY_HIGH))
        .when(upper_sev == "CRITICAL", F.lit(SEVERITY_CRITICAL))
        .when(upper_sev == "ALERT", F.lit(SEVERITY_CRITICAL))
        .when(upper_sev == "EMERGENCY", F.lit(SEVERITY_FATAL))
        .otherwise(F.lit(SEVERITY_UNKNOWN))
    ).cast("int")

# COMMAND ----------

# =============================================================================
# SCC SEVERITY MAPPING — Security Command Center severity to OCSF severity_id
# =============================================================================
# SCC findings use severity values: CRITICAL, HIGH, MEDIUM, LOW, UNSPECIFIED.

def scc_severity_to_ocsf(severity_col):
    """
    Convert SCC finding severity string to OCSF severity_id.
    """
    upper_sev = F.upper(severity_col)
    return (
        F.when(upper_sev == "CRITICAL", F.lit(SEVERITY_CRITICAL))
        .when(upper_sev == "HIGH", F.lit(SEVERITY_HIGH))
        .when(upper_sev == "MEDIUM", F.lit(SEVERITY_MEDIUM))
        .when(upper_sev == "LOW", F.lit(SEVERITY_LOW))
        .otherwise(F.lit(SEVERITY_UNKNOWN))
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
