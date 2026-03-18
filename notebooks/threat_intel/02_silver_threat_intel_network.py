# Databricks notebook source
# -----------------------------------------------------------------------------
# Silver Layer: Threat Intelligence Network Indicator Normalization
# -----------------------------------------------------------------------------
# Reads the latest bronze.threat_intel_raw batch per feed, normalizes IP and
# CIDR indicators, filters private/reserved addresses, and MERGEs results into
# silver.threat_intel_network.
#
# Normalization steps:
#   1. Read only the most recent fetch_timestamp per feed from bronze — avoids
#      reprocessing historical bronze rows on every run.
#   2. Apply parse_network_ioc UDF — validates each raw value, normalizes
#      single IPs to string form and CIDR ranges to canonical CIDR notation.
#   3. Drop rows where UDF returned null (unparseable or unsupported format).
#   4. Filter private/reserved addresses (RFC 1918, loopback, link-local, CGNAT).
#   5. Compute ioc_id = SHA-256(ioc_value || '|' || feed_name) — stable dedup key.
#   6. Apply per-feed metadata: threat_category, confidence_score, TTL, dest_port.
#      IPsum confidence is derived from its per-indicator score; other feeds use
#      their configured default.
#
# MERGE semantics (key: ioc_id):
#   MATCHED     — indicator still appearing in feed → refresh last_seen_at and
#                 expires_at only. Stable fields (confidence, category) are not
#                 updated — feed-level metadata does not change between runs.
#   NOT MATCHED — new indicator → full INSERT with first_seen_at = last_seen_at.
#
# TTL and hard delete:
#   expires_at = last_seen_at + (ttl_days × 2)
#   After each MERGE, a DELETE removes rows where expires_at < current_timestamp().
#   An indicator persists as long as it keeps appearing in feeds. Once a feed
#   stops listing it, last_seen_at stops updating, and the indicator is hard
#   deleted after 2× its TTL.
#
# Deduplication model:
#   Same IP appearing in two different feeds → two rows (intentional).
#   Per-feed provenance, confidence, and TTL are preserved. Cross-feed
#   aggregation happens at query time using GROUP BY ioc_value.
#
# VPC Flow alignment (for future gold-layer correlation):
#   bronze.vpc_flow: src_endpoint.ip / dst_endpoint.ip (STRING, plain IP)
#   This table provides:
#     ioc_value       — exact-match target for single IPs (/32)
#     network_address — CIDR form for ip_in_network() range matching
#     dest_port       — matches dst_endpoint.port (Feodo only; null otherwise)
#     is_network_range — distinguishes which rows need CIDR vs exact matching
#
# DNS and hash placeholder tables are created here to establish schemas
# for future feed additions without requiring schema changes at that time.
#
# Source: security_poc.bronze.threat_intel_raw
# Target: security_poc.silver.threat_intel_network
#         security_poc.silver.threat_intel_dns     (created if not exists)
#         security_poc.silver.threat_intel_hash    (created if not exists)
# Trigger: daily, after bronze_ingest task completes
# -----------------------------------------------------------------------------

# COMMAND ----------

# MAGIC %run ./00_threat_intel_common

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import col, sha2, concat, lit, when

# ═════════════════════════════════════════════════════════════════════════════
# CREATE SILVER TABLES IF NOT EXISTS
# ═════════════════════════════════════════════════════════════════════════════
# Tables are created with explicit DDL before the MERGE so the schema is
# well-defined and downstream consumers (gold notebooks, analysts) can rely
# on it without schema inference. Column comments document the VPC Flow
# alignment for each field.

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {SILVER_TI_NETWORK} (

  -- Primary key: SHA-256 of (ioc_value || '|' || feed_name).
  -- Stable across runs — same IP from the same feed always produces the same key.
  -- Same IP from two different feeds → two separate rows.
  ioc_id           STRING       NOT NULL
                   COMMENT 'SHA-256(ioc_value | feed_name) — primary dedup key',

  -- The normalized indicator value.
  -- Single IPs: plain string, e.g., "1.2.3.4"
  -- CIDR ranges: normalized form, e.g., "1.2.3.0/24"
  -- Join against bronze.vpc_flow src_endpoint.ip / dst_endpoint.ip for exact matches.
  ioc_value        STRING       NOT NULL
                   COMMENT 'Normalized indicator — plain IP string or CIDR range',

  -- Network range classification. True = CIDR prefix < max (e.g., /24 for IPv4).
  -- Use this flag at gold layer to select the correct matching strategy:
  --   is_network_range = False → exact match on ioc_value
  --   is_network_range = True  → ip_in_network(flow_ip, network_address) UDF
  is_network_range BOOLEAN
                   COMMENT 'True if prefix length < max (CIDR range, not single IP)',

  -- Always in CIDR notation, e.g., "1.2.3.4/32" or "1.2.3.0/24".
  -- Use this column as the cidr_str argument to ip_in_network() at gold layer.
  network_address  STRING
                   COMMENT 'CIDR notation — use with ip_in_network() for range matching',

  prefix_length    INT
                   COMMENT 'IP prefix length: 32 = single IPv4; <32 = range',

  ip_version       INT
                   COMMENT '4 = IPv4, 6 = IPv6',

  -- Feed provenance
  feed_name        STRING
                   COMMENT 'Source feed: feodo_tracker, emerging_threats, ipsum',

  threat_category  STRING
                   COMMENT 'c2 (Feodo), compromised (ET), reputation (IPsum)',

  -- Confidence scale: 1=Low, 2=Moderate, 3=High.
  -- Feodo: 3 (High — confirmed C2 server with malware family attribution)
  -- ET:    2 (Moderate — broad compromised/scanning host list)
  -- IPsum: derived from per-indicator score (1 list=Low, 2=Moderate, 3+=High)
  confidence_score INT
                   COMMENT '1=Low  2=Moderate  3=High',

  -- C2 destination port from Feodo. NULL for ET and IPsum (no port data).
  -- Aligns with dst_endpoint.port in bronze.vpc_flow for targeted alerting.
  dest_port        INT
                   COMMENT 'C2 listening port (Feodo only); NULL for other feeds. Matches dst_endpoint.port in bronze.vpc_flow',

  -- Malware family from Feodo (e.g., QakBot, Pikabot). NULL for other feeds.
  malware_family   STRING
                   COMMENT 'Malware family (Feodo only); NULL for other feeds',

  -- Lifecycle timestamps
  first_seen_at    TIMESTAMP
                   COMMENT 'When this indicator was first inserted into this table',

  last_seen_at     TIMESTAMP
                   COMMENT 'When this indicator was last observed in a feed fetch',

  ttl_days         INT
                   COMMENT 'Feed-configured TTL in days; hard delete fires at last_seen_at + (ttl_days * 2)',

  -- Hard delete threshold: last_seen_at + (ttl_days * 2).
  -- To query only currently active indicators: WHERE expires_at > current_timestamp()
  expires_at       TIMESTAMP
                   COMMENT 'Hard delete threshold — last_seen_at + (ttl_days * 2 days)'

) USING DELTA
COMMENT 'Deduplicated, TTL-managed network threat intelligence indicators. Active IOCs: WHERE expires_at > current_timestamp()'
""")

# ─────────────────────────────────────────────────────────────────────────────
# Placeholder tables for future indicator types.
# Schemas defined now so downstream consumers can reference them without
# requiring schema changes when DNS or hash feeds are onboarded.
# ─────────────────────────────────────────────────────────────────────────────

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {SILVER_TI_DNS} (
  ioc_id           STRING    NOT NULL COMMENT 'SHA-256(ioc_value | feed_name)',
  ioc_value        STRING    NOT NULL COMMENT 'Domain or hostname',
  feed_name        STRING,
  threat_category  STRING,
  confidence_score INT       COMMENT '1=Low, 2=Moderate, 3=High',
  first_seen_at    TIMESTAMP,
  last_seen_at     TIMESTAMP,
  ttl_days         INT,
  expires_at       TIMESTAMP COMMENT 'Hard delete threshold — last_seen_at + (ttl_days * 2 days)'
) USING DELTA
COMMENT 'DNS/domain threat intelligence indicators — placeholder, no feeds configured'
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {SILVER_TI_HASH} (
  ioc_id           STRING    NOT NULL COMMENT 'SHA-256(ioc_value | feed_name)',
  ioc_value        STRING    NOT NULL COMMENT 'File hash value',
  hash_type        STRING    COMMENT 'md5, sha1, sha256',
  feed_name        STRING,
  threat_category  STRING,
  confidence_score INT       COMMENT '1=Low, 2=Moderate, 3=High',
  first_seen_at    TIMESTAMP,
  last_seen_at     TIMESTAMP,
  ttl_days         INT,
  expires_at       TIMESTAMP COMMENT 'Hard delete threshold — last_seen_at + (ttl_days * 2 days)'
) USING DELTA
COMMENT 'File hash threat intelligence indicators — placeholder, no feeds configured'
""")

print("Silver tables verified / created.")
print(f"  {SILVER_TI_NETWORK}")
print(f"  {SILVER_TI_DNS}    [placeholder]")
print(f"  {SILVER_TI_HASH}  [placeholder]")

# ═════════════════════════════════════════════════════════════════════════════
# READ LATEST BRONZE BATCH PER FEED
# ═════════════════════════════════════════════════════════════════════════════
# Only process the most recent fetch_timestamp per feed — not all historical
# bronze rows. This ensures the MERGE is fast (processes only today's batch)
# and that last_seen_at reflects actual feed recency, not historical data.
#
# If a feed was skipped by the bronze notebook (e.g., HTTP failure), it will
# be absent from the latest batch. Its silver indicators retain their
# last_seen_at until 2× TTL causes a hard delete.

bronze_df = spark.table(BRONZE_TI_TABLE)
bronze_count = bronze_df.count()

if bronze_count == 0:
    raise RuntimeError(
        f"{BRONZE_TI_TABLE} is empty. "
        "The bronze_ingest task may have failed. Check the prior task logs."
    )

print(f"\nBronze rows available: {bronze_count}")

# Get the most recent fetch timestamp per feed, then inner-join to filter
# bronze to only those rows — one batch per feed for this run.
latest_fetches = bronze_df.groupBy("feed_name").agg(
    F.max("fetch_timestamp").alias("latest_ts")
)

latest_bronze = (
    bronze_df
    .join(
        latest_fetches,
        (bronze_df.feed_name == latest_fetches.feed_name) &
        (bronze_df.fetch_timestamp == latest_fetches.latest_ts),
    )
    # Drop the duplicate feed_name and latest_ts columns from the join key side.
    .drop(latest_fetches.feed_name)
    .drop("latest_ts")
)

latest_count = latest_bronze.count()
print(f"Latest-batch rows to process: {latest_count}")
latest_bronze.groupBy("feed_name").count().show()

# ═════════════════════════════════════════════════════════════════════════════
# PARSE AND VALIDATE: normalize IPs, filter private addresses
# ═════════════════════════════════════════════════════════════════════════════

# Apply parse_network_ioc — returns a struct or null if unparseable.
parsed_df = latest_bronze.withColumn("parsed", parse_network_ioc(col("ioc_raw_value")))

# Drop rows where the UDF returned null (unparseable values, unsupported formats).
valid_df = parsed_df.filter(col("parsed").isNotNull())

# Filter out RFC 1918 / private / reserved addresses.
# is_private_network operates on the normalized CIDR-form network_address.
public_df = valid_df.filter(~is_private_network(col("parsed.network_address")))

# Count filtering effects for observability.
raw_count      = latest_count
parsed_count   = valid_df.count()
public_count   = public_df.count()
invalid_count  = raw_count    - parsed_count
filtered_count = parsed_count - public_count

print(f"\nParsing summary:")
print(f"  Raw rows:             {raw_count}")
print(f"  Valid (parseable):    {parsed_count}  ({invalid_count} dropped — unparseable)")
print(f"  Public (non-RFC1918): {public_count}  ({filtered_count} dropped — private/reserved)")

# ═════════════════════════════════════════════════════════════════════════════
# APPLY FEED METADATA
# ═════════════════════════════════════════════════════════════════════════════
# Enrich each row with threat_category, confidence_score, TTL, dest_port, and
# malware_family based on its feed. IPsum uses per-row score for confidence;
# Feodo and ET use their configured feed-level defaults.
#
# expires_at is computed as: fetch_timestamp + (ttl_days * 2) days.
# Using epoch arithmetic (cast to long, add seconds, cast back) to avoid
# Spark's interval × column limitations.

enriched_df = (
    public_df

    # ── Unpack the parsed struct into individual columns ──
    .withColumn("ioc_value",        col("parsed.ioc_value"))
    .withColumn("is_network_range", col("parsed.is_network_range"))
    .withColumn("network_address",  col("parsed.network_address"))
    .withColumn("prefix_length",    col("parsed.prefix_length"))
    .withColumn("ip_version",       col("parsed.ip_version"))
    .drop("parsed")

    # ── Stable dedup key: SHA-256(ioc_value || '|' || feed_name) ──
    # Consistent across runs so MERGE can reliably match existing rows.
    .withColumn("ioc_id", sha2(concat(col("ioc_value"), lit("|"), col("feed_name")), 256))

    # ── Threat category — one per feed, consistent across all rows ──
    .withColumn("threat_category",
        when(col("feed_name") == "feodo_tracker",    lit("c2"))
        .when(col("feed_name") == "emerging_threats", lit("compromised"))
        .when(col("feed_name") == "ipsum",            lit("reputation"))
        .otherwise(lit("unknown"))
    )

    # ── Confidence score: categorical 1/2/3 ──
    # IPsum: apply categorical normalization from extra_fields["score"].
    #   1 list  = Low (1), 2 lists = Moderate (2), 3+ lists = High (3)
    # Feodo: always 3 (High) — confirmed C2 servers with malware attribution.
    # ET:    always 2 (Moderate) — broad list, some false-positive risk.
    .withColumn("confidence_score",
        when(col("feed_name") == "ipsum",
            when(col("extra_fields")["score"].cast("int") >= 3, lit(3))
            .when(col("extra_fields")["score"].cast("int") == 2, lit(2))
            .otherwise(lit(1))
        )
        .when(col("feed_name") == "feodo_tracker",    lit(3))
        .when(col("feed_name") == "emerging_threats", lit(2))
        .otherwise(lit(1))
    )

    # ── Destination port (Feodo only) ──
    # Feodo provides the port the C2 server listens on. Stored here so the
    # gold-layer correlation can filter on dst_endpoint.port = dest_port
    # to reduce false positives (e.g., skip if flow is on unexpected port).
    .withColumn("dest_port",
        when(
            (col("feed_name") == "feodo_tracker") &
            col("extra_fields")["port"].isNotNull() &
            (col("extra_fields")["port"] != ""),
            col("extra_fields")["port"].cast("int")
        ).otherwise(lit(None).cast("int"))
    )

    # ── Malware family (Feodo only) ──
    .withColumn("malware_family",
        when(
            (col("feed_name") == "feodo_tracker") &
            col("extra_fields")["malware"].isNotNull() &
            (col("extra_fields")["malware"] != ""),
            col("extra_fields")["malware"]
        ).otherwise(lit(None).cast("string"))
    )

    # ── Per-feed TTL (days) ──
    .withColumn("ttl_days",
        when(col("feed_name") == "feodo_tracker",    lit(30))
        .when(col("feed_name") == "emerging_threats", lit(7))
        .when(col("feed_name") == "ipsum",            lit(7))
        .otherwise(lit(7))
    )

    # ── Recency timestamps ──
    .withColumn("last_seen_at", col("fetch_timestamp"))

    # ── expires_at = last_seen_at + (ttl_days * 2) days ──
    # Epoch arithmetic: cast timestamp to unix seconds, add ttl_days*2*86400,
    # cast back. Avoids Spark's INTERVAL × column limitations.
    .withColumn("expires_at",
        (col("fetch_timestamp").cast("long") + col("ttl_days") * 2 * lit(86400))
        .cast("timestamp")
    )
)

# Select only the columns that belong in the silver table.
source_df = enriched_df.select(
    "ioc_id", "ioc_value", "is_network_range", "network_address",
    "prefix_length", "ip_version", "feed_name", "threat_category",
    "confidence_score", "dest_port", "malware_family",
    "last_seen_at", "ttl_days", "expires_at",
)

source_count = source_df.count()
print(f"\nSource rows for MERGE: {source_count}")

# ═════════════════════════════════════════════════════════════════════════════
# MERGE INTO silver.threat_intel_network
# ═════════════════════════════════════════════════════════════════════════════
# Key: ioc_id (SHA-256 of ioc_value + feed_name)
#
# MATCHED: indicator still in feed → update recency timestamps only.
#   Stable fields (threat_category, confidence_score, dest_port, malware_family)
#   are NOT updated — they are feed-level constants that don't change between runs.
#   Updating only timestamps minimizes write amplification on Delta.
#
# NOT MATCHED: new indicator → full INSERT.
#   first_seen_at = last_seen_at on initial insert (we have no earlier record).

source_df.createOrReplaceTempView("ti_network_source")

spark.sql(f"""
MERGE INTO {SILVER_TI_NETWORK} AS t
USING ti_network_source AS s
ON t.ioc_id = s.ioc_id

-- Indicator still in feed: refresh recency only.
WHEN MATCHED THEN UPDATE SET
  t.last_seen_at = s.last_seen_at,
  t.expires_at   = s.expires_at

-- New indicator not previously seen: full insert.
-- first_seen_at initialized to last_seen_at (current fetch timestamp).
WHEN NOT MATCHED THEN INSERT (
  ioc_id, ioc_value, is_network_range, network_address,
  prefix_length, ip_version, feed_name, threat_category,
  confidence_score, dest_port, malware_family,
  first_seen_at, last_seen_at, ttl_days, expires_at
) VALUES (
  s.ioc_id, s.ioc_value, s.is_network_range, s.network_address,
  s.prefix_length, s.ip_version, s.feed_name, s.threat_category,
  s.confidence_score, s.dest_port, s.malware_family,
  s.last_seen_at, s.last_seen_at, s.ttl_days, s.expires_at
)
""")

print("MERGE complete.")

# ═════════════════════════════════════════════════════════════════════════════
# HARD DELETE EXPIRED INDICATORS
# ═════════════════════════════════════════════════════════════════════════════
# Remove indicators whose expires_at has passed (last_seen_at + 2× TTL).
# Runs after every MERGE to keep the table bounded.
#
# An indicator reaches this DELETE only if it has been absent from its feed
# for a full 2× TTL period — meaning the feed itself stopped listing it.
# This is the intended behavior: stale IOCs should not persist indefinitely.

expired_count = spark.sql(f"""
    SELECT COUNT(*) AS cnt
    FROM {SILVER_TI_NETWORK}
    WHERE expires_at < current_timestamp()
""").collect()[0]["cnt"]

spark.sql(f"""
    DELETE FROM {SILVER_TI_NETWORK}
    WHERE expires_at < current_timestamp()
""")

print(f"Hard deleted {expired_count} expired indicators.")

# ═════════════════════════════════════════════════════════════════════════════
# VALIDATION SUMMARY
# ═════════════════════════════════════════════════════════════════════════════

silver = spark.table(SILVER_TI_NETWORK)
total  = silver.count()

print(f"\n{'='*60}")
print(f"silver.threat_intel_network — total rows: {total}")
print(f"{'='*60}")

print("\nBy feed / category / confidence:")
(
    silver
    .groupBy("feed_name", "threat_category", "confidence_score")
    .count()
    .orderBy("feed_name", "confidence_score")
    .show(truncate=False)
)

print("Network range breakdown (is_network_range):")
silver.groupBy("is_network_range").count().show()

print("Feodo indicators with port data (dest_port IS NOT NULL):")
silver.filter(col("dest_port").isNotNull()).select(
    "ioc_value", "dest_port", "malware_family", "confidence_score"
).show(20, truncate=False)

print("Active vs. expiring within 24 hours:")
silver.selectExpr(
    "COUNT(*) AS total_rows",
    "SUM(CASE WHEN expires_at > current_timestamp() THEN 1 ELSE 0 END) AS active",
    "SUM(CASE WHEN expires_at BETWEEN current_timestamp() "
    "    AND current_timestamp() + INTERVAL 1 DAY THEN 1 ELSE 0 END) AS expiring_within_24h",
).show()
