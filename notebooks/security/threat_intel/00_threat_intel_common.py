# Databricks notebook source
# -----------------------------------------------------------------------------
# Threat Intelligence Common — Feed configs, schemas, and IP helper UDFs
# -----------------------------------------------------------------------------
# Loaded via %run by the threat intel pipeline notebooks. Do not run directly.
#
# Provides:
#   FEED_CONFIGS          — per-feed URL, format, TTL, category, default confidence
#   BRONZE_TI_TABLE       — security_poc.security.threat_intel_raw
#   SILVER_TI_NETWORK     — security_poc.security.threat_intel_network
#   SILVER_TI_DNS         — security_poc.security.threat_intel_dns  (placeholder)
#   SILVER_TI_HASH        — security_poc.security.threat_intel_hash (placeholder)
#   BRONZE_TI_SCHEMA      — Spark schema for the bronze table
#   PARSED_IP_SCHEMA      — Return schema for the parse_network_ioc UDF
#   parse_network_ioc()   — UDF: validate + normalize IP/CIDR → struct
#   is_private_network()  — UDF: True if address is RFC 1918 / reserved
#   ip_in_network()       — UDF: True if an IP falls within a CIDR range
#                           (not used in this pipeline; provided for gold-layer
#                            VPC Flow × threat intel correlation)
#
# Confidence score convention (confidence_score INT):
#   1 = Low      (limited corroboration or broad list)
#   2 = Moderate (reputable source or multiple signals)
#   3 = High     (confirmed, dedicated tracker — e.g., Feodo C2)
#
# IPsum maps its per-row list-count score to these categories:
#   source count 1  → 1 (Low)
#   source count 2  → 2 (Moderate)
#   source count 3+ → 3 (High)
#
# VPC Flow alignment:
#   bronze.vpc_flow stores IPs in src_endpoint.ip and dst_endpoint.ip (STRING).
#   security.threat_intel_network provides:
#     ioc_value      — for exact-match joins against single IPs
#     network_address — CIDR form for ip_in_network() range matching
#     dest_port      — matches dst_endpoint.port (Feodo C2 port, when known)
#     is_network_range — distinguishes which rows need CIDR vs exact matching
# -----------------------------------------------------------------------------

from pyspark.sql.types import (
    StructType, StructField,
    StringType, BooleanType, IntegerType, TimestampType, MapType,
)
from pyspark.sql.functions import udf

# ═════════════════════════════════════════════════════════════════════════════
# FEED CONFIGURATIONS
# ═════════════════════════════════════════════════════════════════════════════
# Three no-auth, publicly available threat intelligence feeds selected for
# minimal integration complexity and direct relevance to network-based
# (VPC Flow Log) correlation.
#
# 'format'             — controls which parser is applied in the bronze notebook
# 'ttl_days'           — indicator lifetime; hard delete fires at 2× this value
# 'default_confidence' — applied when the feed has no per-row scoring
#
# Feed selection rationale:
#   feodo_tracker    — Feodo Tracker (abuse.ch): confirmed C2 servers with
#                      malware family and C2 port metadata. Most structured of
#                      the three; Feodo port aligns directly with dst_endpoint.port
#                      in bronze.vpc_flow.
#   emerging_threats — Emerging Threats compromised IP list: broad, widely
#                      recognized list of compromised/scanning hosts. Plain text,
#                      one IP per line.
#   ipsum            — IPsum (stamparm): aggregated reputation list scored by
#                      number of upstream threat lists. Tab-separated IP + count,
#                      giving a per-indicator confidence signal.

FEED_CONFIGS = {
    "feodo_tracker": {
        "url":                "https://feodotracker.abuse.ch/downloads/ipblocklist_aggressive.csv",
        "format":             "csv",
        "ttl_days":           30,
        "threat_category":    "c2",
        "default_confidence": 3,   # High — confirmed C2 IPs with malware family
        "comment":            "Feodo Tracker botnet C2 blocklist (abuse.ch) — CSV with port, malware, status",
    },
    "emerging_threats": {
        "url":                "https://rules.emergingthreats.net/blockrules/compromised-ips.txt",
        "format":             "plaintext",
        "ttl_days":           7,
        "threat_category":    "compromised",
        "default_confidence": 2,   # Moderate — broad compromised host list
        "comment":            "Emerging Threats compromised IP list — plain text, one IP per line",
    },
    "ipsum": {
        "url":                "https://raw.githubusercontent.com/stamparm/ipsum/master/ipsum.txt",
        "format":             "ipsum",
        "ttl_days":           7,
        "threat_category":    "reputation",
        "default_confidence": 1,   # Overridden per-indicator using the feed's score
        "comment":            "IPsum aggregated reputation list — tab-separated IP and list-count score",
    },
}

# ═════════════════════════════════════════════════════════════════════════════
# TABLE NAMES
# ═════════════════════════════════════════════════════════════════════════════

BRONZE_TI_TABLE   = "security_poc.security.threat_intel_raw"
SILVER_TI_NETWORK = "security_poc.security.threat_intel_network"
SILVER_TI_DNS     = "security_poc.security.threat_intel_dns"
SILVER_TI_HASH    = "security_poc.security.threat_intel_hash"

# ═════════════════════════════════════════════════════════════════════════════
# BRONZE TABLE SCHEMA
# ═════════════════════════════════════════════════════════════════════════════
# One row per IOC value extracted from a single feed fetch. 'extra_fields'
# captures per-row metadata from richer feeds (e.g., Feodo provides port and
# malware family per row) without storing the full HTTP response body.

BRONZE_TI_SCHEMA = StructType([
    StructField("ioc_raw_value",       StringType(),                        True),
    StructField("indicator_type_hint", StringType(),                        True),  # ipv4, cidr4, ipv6, cidr6
    StructField("feed_name",           StringType(),                        False),
    StructField("fetch_timestamp",     TimestampType(),                     False),
    StructField("extra_fields",        MapType(StringType(), StringType()), True),
    StructField("_ingested_at",        TimestampType(),                     False),
])

# ═════════════════════════════════════════════════════════════════════════════
# PARSED IP RETURN SCHEMA
# ═════════════════════════════════════════════════════════════════════════════

PARSED_IP_SCHEMA = StructType([
    StructField("ioc_value",        StringType(),  True),  # normalized form (IP or CIDR)
    StructField("is_network_range", BooleanType(), True),  # True if prefix < max_prefix
    StructField("network_address",  StringType(),  True),  # always CIDR notation
    StructField("prefix_length",    IntegerType(), True),  # 32 = single IPv4; <32 = range
    StructField("ip_version",       IntegerType(), True),  # 4 or 6
])

# ═════════════════════════════════════════════════════════════════════════════
# UDF: parse_network_ioc
# ═════════════════════════════════════════════════════════════════════════════

@udf(PARSED_IP_SCHEMA)
def parse_network_ioc(raw_value):
    """
    Validate and normalize a raw IP address or CIDR range string.

    Returns a struct with normalized fields, or None if the value is
    unparseable (null result allows the caller to filter bad rows cleanly).

    Single IPs:    "1.2.3.4"      → ioc_value="1.2.3.4",    network_address="1.2.3.4/32"
    CIDR ranges:   "1.2.3.0/24"   → ioc_value="1.2.3.0/24", network_address="1.2.3.0/24"
    Host-bit CIDR: "1.2.3.4/24"   → normalized to "1.2.3.0/24" (strict=False)

    All imports are inside the function body — required for Databricks serverless
    compute where module-level state is not serialized to executors.
    """
    import ipaddress
    if raw_value is None:
        return None
    try:
        raw = raw_value.strip()
        if "/" in raw:
            # CIDR range — strict=False normalizes host bits (e.g., 1.2.3.4/24 → 1.2.3.0/24)
            net = ipaddress.ip_network(raw, strict=False)
            max_prefix = net.max_prefixlen
            return (
                str(net),                    # ioc_value: normalized CIDR string
                net.prefixlen < max_prefix,  # is_network_range: True for /24, False for /32
                str(net),                    # network_address: same as ioc_value for ranges
                net.prefixlen,               # prefix_length
                net.version,                 # 4 or 6
            )
        else:
            # Single IP address
            addr = ipaddress.ip_address(raw)
            max_prefix = addr.max_prefixlen
            cidr = f"{addr}/{max_prefix}"
            return (
                str(addr),    # ioc_value: plain IP string (no /32 suffix)
                False,        # is_network_range: single IPs are not ranges
                cidr,         # network_address: CIDR form for consistent matching
                max_prefix,   # prefix_length: 32 (IPv4) or 128 (IPv6)
                addr.version, # 4 or 6
            )
    except Exception:
        return None

# ═════════════════════════════════════════════════════════════════════════════
# UDF: is_private_network
# ═════════════════════════════════════════════════════════════════════════════

@udf(BooleanType())
def is_private_network(cidr_str):
    """
    Returns True if the given address or CIDR range overlaps with any
    RFC 1918, loopback, link-local, or CGNAT reserved space.

    Private/reserved ranges filtered out:
      10.0.0.0/8       — RFC 1918 private
      172.16.0.0/12    — RFC 1918 private
      192.168.0.0/16   — RFC 1918 private
      127.0.0.0/8      — Loopback
      169.254.0.0/16   — Link-local (APIPA)
      100.64.0.0/10    — CGNAT / shared address space (RFC 6598)
      ::1/128          — IPv6 loopback
      fc00::/7         — IPv6 unique local addresses

    These are filtered from silver because they carry no threat intel
    signal in a cloud network context where these ranges are internal.
    """
    import ipaddress
    RESERVED = [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
    ]
    if cidr_str is None:
        return False
    try:
        target = ipaddress.ip_network(cidr_str.strip(), strict=False)
        for r in RESERVED:
            try:
                if target.overlaps(ipaddress.ip_network(r)):
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False

# ═════════════════════════════════════════════════════════════════════════════
# UDF: ip_in_network  (gold-layer stub)
# ═════════════════════════════════════════════════════════════════════════════

@udf(BooleanType())
def ip_in_network(ip_str, cidr_str):
    """
    Returns True if ip_str falls within the cidr_str network.

    This UDF is not called by the IOC ingest pipeline. It is defined here
    so the gold-layer VPC Flow × threat intel correlation notebook can load
    it via %run ./00_threat_intel_common without any additional imports.

    Gold-layer usage pattern:
        # Exact match (single IPs — is_network_range = False)
        flow_df.join(
            ti_df.filter(col("is_network_range") == False),
            (col("src_endpoint.ip") == col("ioc_value")) |
            (col("dst_endpoint.ip") == col("ioc_value"))
        )
        # CIDR match (ranges — is_network_range = True)
        flow_df.join(
            ti_df.filter(col("is_network_range") == True),
            ip_in_network(col("src_endpoint.ip"), col("network_address")) |
            ip_in_network(col("dst_endpoint.ip"), col("network_address"))
        )
    """
    import ipaddress
    if ip_str is None or cidr_str is None:
        return False
    try:
        return (
            ipaddress.ip_address(ip_str.strip())
            in ipaddress.ip_network(cidr_str.strip(), strict=False)
        )
    except Exception:
        return False

# ═════════════════════════════════════════════════════════════════════════════
# CONFIDENCE NORMALIZATION
# ═════════════════════════════════════════════════════════════════════════════
# Only used for IPsum (the only feed with per-indicator scoring).
# Feodo and Emerging Threats use their feed-level default_confidence directly.

def normalize_ipsum_confidence(raw_score):
    """
    Map IPsum's raw list-count score to the project confidence scale.
    IPsum score = number of upstream threat lists the IP appears on.
      1  → 1 (Low)
      2  → 2 (Moderate)
      3+ → 3 (High)
    """
    try:
        score = int(raw_score)
        if score >= 3:
            return 3
        elif score == 2:
            return 2
        else:
            return 1
    except Exception:
        return 1  # Default to Low if score is missing or unparseable

# ─────────────────────────────────────────────────────────────────────────────

print("Threat intelligence common helpers loaded.")
print(f"  Configured feeds:  {list(FEED_CONFIGS.keys())}")
print(f"  Bronze table:      {BRONZE_TI_TABLE}")
print(f"  Silver (network):  {SILVER_TI_NETWORK}")
print(f"  Silver (dns):      {SILVER_TI_DNS}    [placeholder — no feeds configured]")
print(f"  Silver (hash):     {SILVER_TI_HASH}   [placeholder — no feeds configured]")
