# Databricks notebook source
# -----------------------------------------------------------------------------
# OCSF Common Helpers for Azure — Shared by all Azure Bronze ingestion notebooks
# -----------------------------------------------------------------------------
# Defines constants, struct builders, and mapping functions for the Open
# Cybersecurity Schema Framework (OCSF) v1.1.0, specialized for Azure data
# sources. Each Azure bronze notebook uses %run ./00_azure_common to import
# these definitions.
#
# This file is intentionally self-contained and does NOT import from the AWS
# common helpers (../aws/00_ocsf_common). The OCSF constants are identical
# across clouds (they are spec-level values), but keeping each cloud's helpers
# independent avoids cross-cloud coupling and allows cloud-specific utilities
# (e.g., Azure resource ID parsing) to live alongside the shared OCSF bits.
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
CLASS_ACCOUNT_CHANGE     = 3001   # IAM / RBAC mutations
CLASS_AUTHENTICATION     = 3002   # Sign-in, MFA, federation
CLASS_NETWORK_ACTIVITY   = 4001   # Network traffic (VNet Flow Logs)
CLASS_API_ACTIVITY       = 6003   # API / resource operations (Activity Log)
CLASS_DETECTION_FINDING  = 2004   # Security findings (Defender for Cloud)

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
# AZURE ACTIVITY LOG EVENT ROUTING — determines which OCSF class an Activity
# Log event maps to based on operationName / category
# =============================================================================

# Sign-in and authentication events -> Authentication (3002)
# Azure Activity Log operationNames and categories that indicate sign-in events.
AUTH_OPERATION_PATTERNS = [
    "Sign",                          # e.g., "Sign In", "Sign-in activity"
    "MICROSOFT.AAD/SIGNIN",          # Azure AD sign-in operations
    "microsoft.aad/signin",
]

AUTH_CATEGORIES = {
    "SignInLogs",
    "NonInteractiveUserSignInLogs",
    "ServicePrincipalSignInLogs",
    "ManagedIdentitySignInLogs",
}

# RBAC / role assignment mutations -> Account Change (3001)
# These operationNames indicate Azure RBAC changes (role assignments, custom
# role definitions, PIM activations, etc.).
ACCOUNT_CHANGE_OPERATION_PATTERNS = [
    "MICROSOFT.AUTHORIZATION/ROLEASSIGNMENTS/",
    "microsoft.authorization/roleAssignments/",
    "MICROSOFT.AUTHORIZATION/ROLEDEFINITIONS/",
    "microsoft.authorization/roleDefinitions/",
    "MICROSOFT.AUTHORIZATION/ELEVATEACCESS/",
    "microsoft.authorization/elevateAccess/",
]

# Everything else -> API Activity (6003) — default for Azure resource operations

# COMMAND ----------

# =============================================================================
# STRUCT BUILDERS — create OCSF nested objects as Spark struct columns
# =============================================================================

def ocsf_metadata(product_name, log_name=None):
    """
    Build the OCSF metadata struct for Azure data sources.
    Required fields: version, product.vendor_name, product.name.
    """
    product_struct = F.struct(
        F.lit("Azure").alias("vendor_name"),
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


def ocsf_cloud(region_col, subscription_uid_col):
    """
    Build the OCSF cloud struct from column references.
    Azure uses subscription IDs where AWS uses account IDs. Both map to
    cloud.account.uid in OCSF.
    """
    return F.struct(
        F.lit("Azure").alias("provider"),
        region_col.alias("region"),
        F.struct(
            subscription_uid_col.cast("string").alias("uid"),
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
# AZURE RESOURCE ID PARSER — extracts structured components from Azure
# resource IDs of the form:
#   /subscriptions/{sub}/resourceGroups/{rg}/providers/{provider}/{type}/{name}
# =============================================================================
# Azure resource IDs are hierarchical strings. This function extracts the key
# components into a Spark struct for use in OCSF cloud.resource fields.
# Non-resource-level IDs (e.g., subscription-level or tenant-level) are handled
# gracefully by returning nulls for missing segments.

def parse_azure_resource_id(resource_id_col):
    """
    Parse an Azure resource ID column into a struct with:
      - subscription_id: the Azure subscription GUID
      - resource_group:  the resource group name
      - provider:        the resource provider (e.g., Microsoft.Compute)
      - resource_type:   the resource type (e.g., virtualMachines)
      - resource_name:   the resource name
      - full_id:         the original resource ID (preserved for unmapped use)
    """
    # Split the resource ID on '/' and extract by position.
    # Format: /subscriptions/{1}/resourceGroups/{3}/providers/{5}/{6}/{7}
    # (indices are after splitting on '/' — first element is empty string)
    parts = F.split(resource_id_col, "/")

    return F.struct(
        # subscriptions is at index 2 (0=empty, 1="subscriptions", 2=<guid>)
        parts.getItem(2).alias("subscription_id"),
        # resourceGroups is at index 4
        parts.getItem(4).alias("resource_group"),
        # provider namespace is at index 6
        parts.getItem(6).alias("provider"),
        # resource type is at index 7
        parts.getItem(7).alias("resource_type"),
        # resource name is at index 8
        parts.getItem(8).alias("resource_name"),
        # Preserve the full ID for unmapped / forensic use
        resource_id_col.alias("full_id"),
    )

# COMMAND ----------

# =============================================================================
# AZURE ACTIVITY LOG HELPERS — derive OCSF activity_id from operation verb
# =============================================================================
# Azure Activity Log operationName follows the pattern:
#   <provider>/<resourceType>/<action>
# where action is typically write, read, delete, action, etc.
# We map these to OCSF activity IDs.

def azure_activity_id(operation_name_col):
    """
    Derive OCSF activity_id from Azure Activity Log operationName.
    The last segment of the operationName typically indicates the verb:
      write  -> CREATE (1)
      read   -> READ (2)
      delete -> DELETE (4)
      action -> UPDATE (3)  (e.g., restart, start, stop)
      other  -> UNKNOWN (0)
    """
    # Extract the last segment (the verb) from the operationName.
    # Azure operationNames look like: Microsoft.Compute/virtualMachines/write
    verb = F.lower(F.element_at(F.split(operation_name_col, "/"), -1))

    return (
        F.when(verb == "write", F.lit(ACTIVITY_CREATE))
        .when(verb == "read", F.lit(ACTIVITY_READ))
        .when(verb == "delete", F.lit(ACTIVITY_DELETE))
        .when(verb == "action", F.lit(ACTIVITY_UPDATE))
        .otherwise(F.lit(ACTIVITY_UNKNOWN))
    ).cast("int")

# COMMAND ----------

# =============================================================================
# AZURE SEVERITY MAPPING — Activity Log level to OCSF severity_id
# =============================================================================
# Azure Activity Log 'level' field uses string values: Informational, Warning,
# Error, Critical. Map these to OCSF severity IDs.

def azure_level_to_severity(level_col):
    """
    Convert Azure Activity Log level string to OCSF severity_id.
    """
    return (
        F.when(F.upper(level_col) == "INFORMATIONAL", F.lit(SEVERITY_INFORMATIONAL))
        .when(F.upper(level_col) == "WARNING", F.lit(SEVERITY_MEDIUM))
        .when(F.upper(level_col) == "ERROR", F.lit(SEVERITY_HIGH))
        .when(F.upper(level_col) == "CRITICAL", F.lit(SEVERITY_CRITICAL))
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
