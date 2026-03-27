# Databricks notebook source
# -----------------------------------------------------------------------------
# Gold Layer: EC2 Current-State Inventory
# -----------------------------------------------------------------------------
# Builds a current-state inventory of EC2 instances enriched with related
# resources: network interfaces, volumes, security groups, subnets, and VPCs.
#
# Unlike the silver CDC table (append-only, every change), this gold table
# maintains ONE ROW PER INSTANCE reflecting the most recent known state.
# It uses MERGE (upsert) semantics: new instances are inserted, existing
# instances are updated when a newer capture_time arrives.
#
# Data flow:
#   silver.config_cdc (all resource types, all changes)
#     → filter to EC2-related resource types
#     → get latest state per resource (window function on capture_time)
#     → parse configuration JSON for core fields
#     → join related resources onto each instance
#     → MERGE into gold.ec2_inventory on (aws_account_id, instance_id)
#
# Related resource types joined:
#   - AWS::EC2::NetworkInterface → private/public IPs, MAC, attachment info
#   - AWS::EC2::Volume → size, type, encrypted, attachment state
#   - AWS::EC2::SecurityGroup → group name, description, rules summary
#   - AWS::EC2::Subnet → CIDR, AZ, available IPs
#   - AWS::EC2::VPC → CIDR, DHCP options, tenancy
#
# The relationships column in the CDC table links instances to their related
# resources via (resourceType, resourceId) pairs. We use these to join.
#
# Source table: security_poc.silver.config_cdc
# Target table: security_poc.gold.ec2_inventory
#
# Parameters (passed via job or widgets):
#   (none — reads directly from silver table, no checkpoints needed)
# -----------------------------------------------------------------------------

# COMMAND ----------

source_table = "security_poc.silver.config_cdc"
target_table = "security_poc.gold.ec2_inventory"

print(f"Source: {source_table}")
print(f"Target: {target_table}")

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.functions import (
    col, current_timestamp, lit, row_number, from_json, get_json_object,
    collect_list, struct, when, coalesce,
)
from pyspark.sql.window import Window
from pyspark.sql.types import ArrayType, StructType, StructField, StringType

# =============================================================================
# STEP 1 — Get latest state for every resource from silver CDC
# =============================================================================
# The CDC table has multiple rows per resource (one per change). We want only
# the most recent capture for each (aws_account_id, resource_type, resource_id).
# Window function partitioned by resource identity, ordered by capture_time DESC.

cdc = spark.table(source_table)

# Window: latest capture per unique resource.
latest_window = Window.partitionBy(
    "aws_account_id", "resource_type", "resource_id"
).orderBy(col("capture_time").desc())

latest_state = (
    cdc
    .withColumn("_rn", row_number().over(latest_window))
    .filter(col("_rn") == 1)
    .drop("_rn")
)

# Materialize as a temp view so Spark can optimize across multiple reads.
# Note: .cache() / .persist() are not supported on serverless compute.
latest_state.createOrReplaceTempView("latest_state")

print(f"Unique resources (latest state): {spark.table('latest_state').count()}")

# COMMAND ----------

# =============================================================================
# STEP 2 — Extract EC2 instances and parse configuration JSON
# =============================================================================
# The configuration column for AWS::EC2::Instance contains the full EC2
# DescribeInstances output as a JSON string. We extract the most useful fields.

instances = spark.table("latest_state").filter(col("resource_type") == "AWS::EC2::Instance")

# Parse key fields from the configuration JSON string.
# Using get_json_object for safe extraction from variable-structure JSON.
ec2_base = instances.select(
    # ── Primary key ──
    col("aws_account_id"),
    col("resource_id").alias("instance_id"),

    # ── Identity ──
    col("arn"),
    col("resource_name"),

    # ── Location ──
    col("aws_region"),
    col("availability_zone"),

    # ── Instance properties (from configuration JSON) ──
    get_json_object(col("configuration"), "$.instanceType").alias("instance_type"),
    get_json_object(col("configuration"), "$.imageId").alias("image_id"),
    get_json_object(col("configuration"), "$.keyName").alias("key_name"),
    # AWS Config stores nested objects as escaped JSON strings, so $.state
    # is a string like '{"code":16,"name":"running"}', not a nested object.
    # Double get_json_object: first to get the state string, then to parse it.
    get_json_object(
        get_json_object(col("configuration"), "$.state"), "$.name"
    ).alias("instance_state"),
    get_json_object(col("configuration"), "$.launchTime").cast("timestamp").alias("launch_time"),
    get_json_object(col("configuration"), "$.platform").alias("platform"),
    get_json_object(col("configuration"), "$.architecture").alias("architecture"),
    get_json_object(col("configuration"), "$.rootDeviceType").alias("root_device_type"),
    get_json_object(col("configuration"), "$.rootDeviceName").alias("root_device_name"),
    get_json_object(col("configuration"), "$.privateDnsName").alias("private_dns_name"),
    get_json_object(col("configuration"), "$.privateIpAddress").alias("private_ip"),
    get_json_object(col("configuration"), "$.publicDnsName").alias("public_dns_name"),
    get_json_object(col("configuration"), "$.publicIpAddress").alias("public_ip"),
    get_json_object(col("configuration"), "$.vpcId").alias("vpc_id"),
    get_json_object(col("configuration"), "$.subnetId").alias("subnet_id"),

    # ── IAM ──
    get_json_object(col("configuration"), "$.iamInstanceProfile.arn").alias("iam_instance_profile_arn"),

    # ── CDC metadata ──
    col("change_type"),
    col("capture_time").alias("last_capture_time"),
    col("resource_creation_time"),

    # ── Tags and relationships (kept for joining) ──
    col("tags"),
    col("relationships"),
    col("configuration"),
)

print(f"EC2 instances found: {ec2_base.count()}")

# COMMAND ----------

# =============================================================================
# STEP 3 — Parse relationships to find related resource IDs
# =============================================================================
# The relationships column is a JSON array of objects like:
#   [{"resourceType":"AWS::EC2::NetworkInterface","resourceId":"eni-xxx",
#     "resourceName":null,"name":"Contains NetworkInterface"}, ...]
#
# We explode this to get (instance_id, related_resource_type, related_resource_id)
# then join with the latest state of each related resource.

relationships_schema = ArrayType(StructType([
    StructField("resourceType", StringType()),
    StructField("resourceId", StringType()),
    StructField("resourceName", StringType()),
    StructField("name", StringType()),
]))

instance_rels = (
    ec2_base
    .select(
        col("aws_account_id"),
        col("instance_id"),
        F.explode(
            from_json(col("relationships"), relationships_schema)
        ).alias("rel"),
    )
    .select(
        col("aws_account_id"),
        col("instance_id"),
        col("rel.resourceType").alias("rel_resource_type"),
        col("rel.resourceId").alias("rel_resource_id"),
        col("rel.name").alias("rel_name"),
    )
)

# COMMAND ----------

# =============================================================================
# STEP 4 — Build enrichment DataFrames for each related resource type
# =============================================================================
# For each resource type we want to enrich with, we:
#   1. Filter latest_state to that resource type
#   2. Parse the configuration JSON for relevant fields
#   3. Join to instance_rels to link back to the parent instance
#   4. Aggregate into an array of structs per instance

# ── 4a. Network Interfaces ──
enis_raw = spark.table("latest_state").filter(col("resource_type") == "AWS::EC2::NetworkInterface")
enis = enis_raw.select(
    col("aws_account_id"),
    col("resource_id").alias("eni_id"),
    get_json_object(col("configuration"), "$.description").alias("description"),
    get_json_object(col("configuration"), "$.privateIpAddress").alias("private_ip"),
    get_json_object(col("configuration"), "$.association.publicIp").alias("public_ip"),
    get_json_object(col("configuration"), "$.macAddress").alias("mac_address"),
    get_json_object(col("configuration"), "$.status").alias("status"),
    get_json_object(col("configuration"), "$.subnetId").alias("subnet_id"),
    get_json_object(col("configuration"), "$.vpcId").alias("vpc_id"),
)

# Join ENIs to instances via relationships.
instance_enis = (
    instance_rels
    .filter(col("rel_resource_type") == "AWS::EC2::NetworkInterface")
    .join(enis,
          (instance_rels["aws_account_id"] == enis["aws_account_id"]) &
          (instance_rels["rel_resource_id"] == enis["eni_id"]),
          "inner")
    .groupBy(instance_rels["aws_account_id"], "instance_id")
    .agg(
        collect_list(
            struct(
                col("eni_id"),
                col("description"),
                enis["private_ip"],
                enis["public_ip"],
                col("mac_address"),
                enis["status"],
                enis["subnet_id"],
                enis["vpc_id"],
            )
        ).alias("network_interfaces")
    )
)

# ── 4b. Volumes ──
volumes_raw = spark.table("latest_state").filter(col("resource_type") == "AWS::EC2::Volume")
volumes = volumes_raw.select(
    col("aws_account_id"),
    col("resource_id").alias("volume_id"),
    get_json_object(col("configuration"), "$.size").cast("int").alias("size_gb"),
    get_json_object(col("configuration"), "$.volumeType").alias("volume_type"),
    get_json_object(col("configuration"), "$.encrypted").cast("boolean").alias("encrypted"),
    get_json_object(col("configuration"), "$.state").alias("state"),
    get_json_object(col("configuration"), "$.iops").cast("int").alias("iops"),
)

instance_volumes = (
    instance_rels
    .filter(col("rel_resource_type") == "AWS::EC2::Volume")
    .join(volumes,
          (instance_rels["aws_account_id"] == volumes["aws_account_id"]) &
          (instance_rels["rel_resource_id"] == volumes["volume_id"]),
          "inner")
    .groupBy(instance_rels["aws_account_id"], "instance_id")
    .agg(
        collect_list(
            struct(
                col("volume_id"),
                col("size_gb"),
                col("volume_type"),
                col("encrypted"),
                col("state"),
                col("iops"),
            )
        ).alias("volumes")
    )
)

# ── 4c. Security Groups ──
sgs_raw = spark.table("latest_state").filter(col("resource_type") == "AWS::EC2::SecurityGroup")
sgs = sgs_raw.select(
    col("aws_account_id"),
    col("resource_id").alias("sg_id"),
    get_json_object(col("configuration"), "$.groupName").alias("group_name"),
    get_json_object(col("configuration"), "$.description").alias("description"),
    get_json_object(col("configuration"), "$.vpcId").alias("vpc_id"),
    # Keep full rules as JSON — too variable to normalize here.
    get_json_object(col("configuration"), "$.ipPermissions").alias("inbound_rules"),
    get_json_object(col("configuration"), "$.ipPermissionsEgress").alias("outbound_rules"),
)

instance_sgs = (
    instance_rels
    .filter(col("rel_resource_type") == "AWS::EC2::SecurityGroup")
    .join(sgs,
          (instance_rels["aws_account_id"] == sgs["aws_account_id"]) &
          (instance_rels["rel_resource_id"] == sgs["sg_id"]),
          "inner")
    .groupBy(instance_rels["aws_account_id"], "instance_id")
    .agg(
        collect_list(
            struct(
                col("sg_id"),
                col("group_name"),
                sgs["description"],
                sgs["vpc_id"],
                col("inbound_rules"),
                col("outbound_rules"),
            )
        ).alias("security_groups")
    )
)

# ── 4d. Subnet ──
# Subnets are joined via the subnet_id extracted from the instance config
# (not via relationships, since the relationship is "Is contained in Subnet").
subnets_raw = spark.table("latest_state").filter(col("resource_type") == "AWS::EC2::Subnet")
subnets = subnets_raw.select(
    col("aws_account_id"),
    col("resource_id").alias("subnet_resource_id"),
    get_json_object(col("configuration"), "$.subnetId").alias("subnet_id"),
    get_json_object(col("configuration"), "$.cidrBlock").alias("subnet_cidr"),
    get_json_object(col("configuration"), "$.availableIpAddressCount").cast("int").alias("available_ips"),
    get_json_object(col("configuration"), "$.availabilityZone").alias("subnet_az"),
)

# ── 4e. VPC ──
vpcs_raw = spark.table("latest_state").filter(col("resource_type") == "AWS::EC2::VPC")
vpcs = vpcs_raw.select(
    col("aws_account_id"),
    col("resource_id").alias("vpc_resource_id"),
    get_json_object(col("configuration"), "$.vpcId").alias("vpc_id"),
    get_json_object(col("configuration"), "$.cidrBlock").alias("vpc_cidr"),
    get_json_object(col("configuration"), "$.instanceTenancy").alias("vpc_tenancy"),
    get_json_object(col("configuration"), "$.isDefault").cast("boolean").alias("is_default_vpc"),
)

# COMMAND ----------

# =============================================================================
# STEP 5 — Join everything onto the EC2 base table
# =============================================================================
# Left joins ensure instances without certain related resources still appear.
# We use DataFrame aliases to avoid ambiguous column references after joins.

base = ec2_base.alias("base")

# Join enrichment arrays — each uses aliased DataFrames to disambiguate.
ec2_enriched = base

# Network interfaces
eni_df = instance_enis.alias("eni")
ec2_enriched = (
    ec2_enriched.alias("e")
    .join(eni_df,
          (col("e.aws_account_id") == col("eni.aws_account_id")) &
          (col("e.instance_id") == col("eni.instance_id")),
          "left")
    .select("e.*", col("eni.network_interfaces"))
)

# Volumes
vol_df = instance_volumes.alias("vol")
ec2_enriched = (
    ec2_enriched.alias("e")
    .join(vol_df,
          (col("e.aws_account_id") == col("vol.aws_account_id")) &
          (col("e.instance_id") == col("vol.instance_id")),
          "left")
    .select("e.*", col("vol.volumes"))
)

# Security groups
sg_df = instance_sgs.alias("sg")
ec2_enriched = (
    ec2_enriched.alias("e")
    .join(sg_df,
          (col("e.aws_account_id") == col("sg.aws_account_id")) &
          (col("e.instance_id") == col("sg.instance_id")),
          "left")
    .select("e.*", col("sg.security_groups"))
)

# Subnet (joined via subnet_id from instance config, not relationships)
sub_df = subnets.alias("sub")
ec2_enriched = (
    ec2_enriched.alias("e")
    .join(sub_df,
          (col("e.aws_account_id") == col("sub.aws_account_id")) &
          (col("e.subnet_id") == col("sub.subnet_id")),
          "left")
    .select("e.*", col("sub.subnet_cidr"), col("sub.subnet_az"), col("sub.available_ips"))
)

# VPC (joined via vpc_id from instance config)
vpc_df = vpcs.alias("vpc")
ec2_enriched = (
    ec2_enriched.alias("e")
    .join(vpc_df,
          (col("e.aws_account_id") == col("vpc.aws_account_id")) &
          (col("e.vpc_id") == col("vpc.vpc_id")),
          "left")
    .select("e.*", col("vpc.vpc_cidr"), col("vpc.vpc_tenancy"), col("vpc.is_default_vpc"))
)

# COMMAND ----------

# =============================================================================
# STEP 6 — Select final columns and MERGE into gold table
# =============================================================================
# We select the final schema explicitly to control column order and naming.
# Empty arrays are replaced with empty typed arrays for schema consistency.

ec2_final = ec2_enriched.select(
    # ── Primary key ──
    col("aws_account_id"),
    col("instance_id"),

    # ── Identity ──
    col("arn"),
    col("resource_name"),

    # ── Location ──
    col("aws_region"),
    col("availability_zone"),

    # ── Instance properties ──
    col("instance_type"),
    col("instance_state"),
    col("image_id"),
    col("key_name"),
    col("platform"),
    col("architecture"),
    col("root_device_type"),
    col("root_device_name"),
    col("launch_time"),

    # ── Networking (from instance config + joined subnet/VPC) ──
    col("private_ip"),
    col("private_dns_name"),
    col("public_ip"),
    col("public_dns_name"),
    col("vpc_id"),
    col("vpc_cidr"),
    col("vpc_tenancy"),
    col("is_default_vpc"),
    col("subnet_id"),
    col("subnet_cidr"),
    col("subnet_az"),
    col("available_ips").alias("subnet_available_ips"),

    # ── IAM ──
    col("iam_instance_profile_arn"),

    # ── Related resources (arrays of structs) ──
    coalesce(
        col("network_interfaces"),
        F.array().cast("array<struct<eni_id:string,description:string,private_ip:string,public_ip:string,mac_address:string,status:string,subnet_id:string,vpc_id:string>>")
    ).alias("network_interfaces"),
    coalesce(
        col("volumes"),
        F.array().cast("array<struct<volume_id:string,size_gb:int,volume_type:string,encrypted:boolean,state:string,iops:int>>")
    ).alias("volumes"),
    coalesce(
        col("security_groups"),
        F.array().cast("array<struct<sg_id:string,group_name:string,description:string,vpc_id:string,inbound_rules:string,outbound_rules:string>>")
    ).alias("security_groups"),

    # ── Tags ──
    col("tags"),

    # ── CDC metadata ──
    col("change_type").alias("last_change_type"),
    col("last_capture_time"),
    col("resource_creation_time"),

    # ── Raw configuration (for ad-hoc analysis) ──
    col("configuration").alias("raw_configuration"),
    col("relationships").alias("raw_relationships"),

    # ── Gold ingestion metadata ──
    current_timestamp().alias("_ingested_at"),
)

# COMMAND ----------

# =============================================================================
# MERGE — upsert into gold table
# =============================================================================
# First run: table doesn't exist yet, so we create it.
# Subsequent runs: MERGE on (aws_account_id, instance_id) — update if the
# incoming capture_time is newer, insert if the instance is new.
#
# We use createOrReplaceTempView + MERGE SQL for clarity.

ec2_final.createOrReplaceTempView("ec2_updates")

# Check if the gold table exists. If not, create it from the first batch.
if not spark.catalog.tableExists(target_table):
    print(f"Creating {target_table} (first run)...")
    ec2_final.write.format("delta").mode("overwrite").saveAsTable(target_table)
    print(f"Created with {spark.table(target_table).count()} rows.")
else:
    # MERGE: update existing instances if we have a newer capture, insert new ones.
    merge_sql = f"""
    MERGE INTO {target_table} AS target
    USING ec2_updates AS source
    ON target.aws_account_id = source.aws_account_id
       AND target.instance_id = source.instance_id
    WHEN MATCHED AND source.last_capture_time > target.last_capture_time THEN
        UPDATE SET *
    WHEN NOT MATCHED THEN
        INSERT *
    """
    spark.sql(merge_sql)
    print(f"Merged into {target_table}. Total rows: {spark.table(target_table).count()}")

# COMMAND ----------

# =============================================================================
# VALIDATION — verify the gold table
# =============================================================================

inventory = spark.table(target_table)

print(f"\nTotal instances: {inventory.count()}")

print(f"\nInstances by account:")
inventory.groupBy("aws_account_id").count().show()

print(f"\nInstances by state:")
inventory.groupBy("instance_state").count().show()

print(f"\nInstances by type:")
inventory.groupBy("instance_type").count().show()

print(f"\nInstances by region/AZ:")
inventory.groupBy("aws_region", "availability_zone").count().show()

print(f"\nSample inventory:")
inventory.select(
    "aws_account_id", "instance_id", "instance_type", "instance_state",
    "private_ip", "vpc_id", "subnet_id", "last_capture_time",
    F.size("network_interfaces").alias("eni_count"),
    F.size("volumes").alias("volume_count"),
    F.size("security_groups").alias("sg_count"),
).show(truncate=False)
