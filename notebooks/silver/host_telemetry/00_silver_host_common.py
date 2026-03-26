# Databricks notebook source
# -----------------------------------------------------------------------------
# Silver Host Common — Shared helpers for host telemetry normalization
# -----------------------------------------------------------------------------
# Provides constants, schema definitions, and utility functions used by all
# silver host telemetry notebooks. Imported via %run ./00_silver_host_common.
#
# Silver host normalization reads from 5 bronze OCSF tables and produces 4
# semantic tables organized by event meaning (auth, process execution, account
# change, system events) rather than by source type or OS.
#
# Key responsibilities:
#   - event_id computation (SHA-256 dedup key)
#   - Routing label extraction with regex fallback for historical data
#   - try_optimize wrapper (same as bronze, reused here for DRY)
# -----------------------------------------------------------------------------

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, lit, when, current_timestamp, sha2, concat, substring

# COMMAND ----------

# =============================================================================
# EVENT ID — stable dedup key for the unified timeline
# =============================================================================
# Computed as SHA-256 of (source_table + time + hostname + message[:256]).
# Using a truncated message avoids hashing large raw_event strings while
# still providing collision resistance across events with the same timestamp
# on the same host.

def compute_event_id(source_table_name, hostname_col="device.hostname"):
    """
    Returns a Column expression computing SHA-256(source_table + time + hostname + message[:256]).

    Args:
        source_table_name: String literal identifying the silver table (e.g., "host_authentications").
        hostname_col: Column name containing the hostname (default "device.hostname" for bronze tables).
    """
    return sha2(
        concat(
            lit(source_table_name),
            col("time").cast("string"),
            col(hostname_col),
            substring(col("message"), 1, 256),
        ),
        256
    ).alias("event_id")

# COMMAND ----------

# =============================================================================
# ROUTING LABEL EXTRACTION — read Cribl-added labels from metadata.labels
# with regex fallback for historical bronze data that lacks the labels.
# =============================================================================

def get_audit_type(message_col="message"):
    """
    Extract the audit_type routing label from metadata.labels, falling back
    to regex on the message column for historical data.

    Returns a Column expression with values: 'process', 'auth', 'account', 'system'.
    """
    return (
        when(col("metadata.labels.audit_type").isNotNull() &
             (col("metadata.labels.audit_type") != "unknown"),
             col("metadata.labels.audit_type"))
        .when(F.regexp_extract(col(message_col), r"type=(EXECVE|SYSCALL)", 1) != "",
              lit("process"))
        .when(F.regexp_extract(col(message_col), r"type=(USER_AUTH|USER_LOGIN|CRED_ACQ)", 1) != "",
              lit("auth"))
        .when(F.regexp_extract(col(message_col), r"type=(ADD_USER|DEL_USER|USER_ACCT)", 1) != "",
              lit("account"))
        .otherwise(lit("system"))
    )


def get_win_event_type(message_col="message"):
    """
    Extract the win_event_type routing label from metadata.labels, falling back
    to EventCode regex on the message column for historical data.

    Returns a Column expression with values: 'auth', 'process', 'account', 'system'.
    """
    # Extract EventCode from message for fallback
    event_code_col = (
        when(
            F.regexp_extract(col(message_col), r"EventCode=(\d+)", 1) != "",
            F.regexp_extract(col(message_col), r"EventCode=(\d+)", 1).cast("int")
        )
    )

    return (
        when(col("metadata.labels.win_event_type").isNotNull() &
             (col("metadata.labels.win_event_type") != "unknown"),
             col("metadata.labels.win_event_type"))
        .when(event_code_col.isin(4624, 4625, 4634, 4647, 4648), lit("auth"))
        .when(event_code_col.isin(4688), lit("process"))
        .when(event_code_col.isin(4720, 4722, 4723, 4724, 4725, 4726), lit("account"))
        .otherwise(lit("system"))
    )

# COMMAND ----------

# =============================================================================
# UTILITY — OPTIMIZE + ZORDER (reused from bronze host_common pattern)
# =============================================================================

def try_optimize(spark, table_name, zorder_cols):
    """
    Attempt to run OPTIMIZE with ZORDER on the given Delta table.
    Catches and logs errors on runtimes that don't support OPTIMIZE.
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
