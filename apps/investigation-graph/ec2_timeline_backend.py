# -----------------------------------------------------------------------------
# EC2 Config Timeline Backend — CloudTrail + Config CDC timeline for EC2 instances
# -----------------------------------------------------------------------------
# Host-centric investigation: enumerate EC2 instances, fetch instance details,
# classify the originating source of CloudTrail API calls, and build a merged
# timeline from CloudTrail + AWS Config CDC.  Follows the same conventions as
# backend.py: CATALOG constant, dict rows, try/except per query,
# TABLE_OR_VIEW_NOT_FOUND suppression, no .cache()/.persist().
# -----------------------------------------------------------------------------

import json
import re
from datetime import datetime, timedelta

CATALOG = "security_poc"


# ---------------------------------------------------------------------------
# Source classification
# ---------------------------------------------------------------------------

def classify_source(user_agent):
    """
    Parse a CloudTrail http_request.user_agent string and return a dict
    describing the originating tool/source.

    Patterns are checked in a specific order because Terraform user agents
    contain "aws-sdk" in addition to "Terraform", so the Terraform check
    must come first.

    Returns: {"source_type": str, "label": str, "color": str}
    """
    if not user_agent:
        return {"source_type": "unknown", "label": "Unknown", "color": "#f8f8f2"}

    ua = user_agent  # keep original; comparisons are case-sensitive per AWS convention

    # 1. Terraform — must precede aws-sdk check (Terraform embeds aws-sdk in UA)
    if "Terraform" in ua:
        return {"source_type": "iac", "label": "IaC", "color": "#8be9fd"}

    # 2. AWS Management Console
    if "console.aws.amazon.com" in ua:
        return {"source_type": "console", "label": "Console", "color": "#ffb86c"}

    # 3. AWS CLI
    if "aws-cli" in ua:
        return {"source_type": "cli", "label": "CLI", "color": "#50fa7b"}

    # 4. boto3 / AWS SDK (includes CDK, SAM, and other SDK-based tools)
    if "boto3" in ua or "aws-sdk" in ua:
        return {"source_type": "sdk", "label": "Script", "color": "#bd93f9"}

    # 5. AWS service principals (config.amazonaws.com, resource-explorer, etc.)
    if ua.endswith(".amazonaws.com"):
        return {"source_type": "service", "label": "Service", "color": "#6272a4"}

    # 6. Catch-all
    return {"source_type": "unknown", "label": "Unknown", "color": "#f8f8f2"}


# ---------------------------------------------------------------------------
# Instance enumeration and detail
# ---------------------------------------------------------------------------

def list_instances(conn):
    """
    Return a list of EC2 instances from gold.ec2_inventory.

    Parses the JSON tags string to extract the Name tag for display.
    Returns a list of dicts with keys: instance_id, name, account_id,
    region, state.  Returns an empty list on any query failure.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT instance_id, private_ip, public_ip, instance_state,
                       instance_type, aws_account_id, aws_region, tags
                FROM {CATALOG}.gold.ec2_inventory
                ORDER BY instance_id
            """)
            cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        instances = []
        for row in rows:
            # Parse tags JSON to extract the Name tag (may be absent)
            try:
                tags = json.loads(row["tags"]) if row["tags"] else {}
            except (json.JSONDecodeError, TypeError):
                tags = {}
            name = tags.get("Name", "")

            instances.append({
                "instance_id": row["instance_id"],
                "name": name,
                "account_id": row["aws_account_id"],
                "region": row["aws_region"],
                "state": row["instance_state"],
            })
        return instances
    except Exception:
        return []


def get_instance_details(conn, instance_id):
    """
    Return full detail for a single EC2 instance, or None if not found.

    Tags are returned as a parsed dict.  Security groups are parsed from a
    JSON array of structs and returned as a list of group_name strings.
    Returns None on any query failure or if the instance is not in inventory.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT instance_id, private_ip, public_ip, private_dns_name,
                       public_dns_name, instance_state, instance_type, key_name,
                       launch_time, aws_account_id, aws_region,
                       availability_zone, tags, security_groups
                FROM {CATALOG}.gold.ec2_inventory
                WHERE instance_id = %(instance_id)s
            """, {"instance_id": instance_id})
            cols = [desc[0] for desc in cur.description]
            row = cur.fetchone()

        if not row:
            return None

        data = dict(zip(cols, row))

        # Parse tags JSON string → dict
        try:
            data["tags"] = json.loads(data["tags"]) if data["tags"] else {}
        except (json.JSONDecodeError, TypeError):
            data["tags"] = {}

        # Parse security_groups JSON array of structs → list of group_name strings
        try:
            sg_raw = json.loads(data["security_groups"]) if data["security_groups"] else []
            # Each element is a struct; extract group_name where present
            data["security_groups"] = [
                sg.get("group_name", sg.get("groupName", str(sg)))
                for sg in sg_raw
                if isinstance(sg, dict)
            ]
        except (json.JSONDecodeError, TypeError):
            data["security_groups"] = []

        return data
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Timeline construction helpers
# ---------------------------------------------------------------------------

def _extract_instance_ids_from_raw(raw_data_str):
    """
    Parse raw CloudTrail JSON and return the set of EC2 instance IDs referenced
    in the event's request/response parameters.

    Checks the following locations:
      - requestParameters.instanceId
      - requestParameters.instancesSet.items[].instanceId
      - responseElements.instancesSet.items[].instanceId
      - requestParameters.resourcesSet.items[].resourceId

    Returns a set of strings (may be empty if the event carries no instance
    references, e.g., DescribeInstances with no filter).
    """
    if not raw_data_str:
        return set()

    try:
        raw = json.loads(raw_data_str)
    except (json.JSONDecodeError, TypeError):
        return set()

    found = set()

    req = raw.get("requestParameters") or {}
    resp = raw.get("responseElements") or {}

    # Single instance reference
    single = req.get("instanceId")
    if single:
        found.add(single)

    # instancesSet in requestParameters
    req_set = req.get("instancesSet") or {}
    for item in req_set.get("items", []):
        iid = item.get("instanceId")
        if iid:
            found.add(iid)

    # instancesSet in responseElements
    resp_set = resp.get("instancesSet") or {}
    for item in resp_set.get("items", []):
        iid = item.get("instanceId")
        if iid:
            found.add(iid)

    # resourcesSet (used by some EC2 API calls, e.g., CreateTags)
    res_set = req.get("resourcesSet") or {}
    for item in res_set.get("items", []):
        rid = item.get("resourceId")
        if rid:
            found.add(rid)

    return found


# ---------------------------------------------------------------------------
# Primary timeline builder
# ---------------------------------------------------------------------------

def build_ec2_timeline(conn, instance_id, start_time, end_time):
    """
    Build a merged, time-sorted timeline of CloudTrail API calls and AWS Config
    CDC changes for a specific EC2 instance.

    CloudTrail events are filtered to those that reference instance_id explicitly
    in their request/response parameters.  Events with no parseable instance
    references (e.g., broad DescribeInstances) are included as account-wide
    context — the caller may choose to show or hide them via is_read_only.

    Returns a list of event dicts sorted by "time" (string).

    Each CloudTrail event dict contains:
        time, event_type="cloudtrail", operation, who, user_type, source_ip,
        user_agent, source (from classify_source), status, status_detail,
        is_failure, is_read_only, detail

    Each Config CDC event dict contains:
        time, event_type="config", operation, who, user_type, source_ip,
        user_agent, source, change_type, status, status_detail,
        is_failure, is_read_only, detail

    Both queries are wrapped individually so a missing table produces an empty
    result for that source rather than a full failure.
    """
    params = {
        "start": str(start_time),
        "end": str(end_time),
        "instance_id": instance_id,
    }

    cloudtrail_events = []

    # ------------------------------------------------------------------
    # Query 1: CloudTrail — all EC2 API calls in the time window
    # ------------------------------------------------------------------
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT time,
                       api.operation          AS operation,
                       api.service.name       AS service,
                       actor.user.name        AS who,
                       actor.user.type        AS user_type,
                       actor.session.issuer   AS session_issuer,
                       src_endpoint.ip        AS source_ip,
                       http_request.user_agent AS user_agent,
                       status,
                       status_detail,
                       raw_data
                FROM {CATALOG}.bronze.cloudtrail
                WHERE api.service.name = 'ec2.amazonaws.com'
                  AND time BETWEEN %(start)s AND %(end)s
                ORDER BY time ASC
            """, params)
            cols = [desc[0] for desc in cur.description]
            ct_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        # Suppress TABLE_OR_VIEW_NOT_FOUND; treat any failure as empty result
        if "TABLE_OR_VIEW_NOT_FOUND" not in str(exc):
            pass  # still return empty for this source
        ct_rows = []

    for row in ct_rows:
        raw_data_str = row.get("raw_data") or ""
        extracted_ids = _extract_instance_ids_from_raw(raw_data_str)

        # Include the event if:
        #   (a) the target instance_id is explicitly referenced, OR
        #   (b) the event carries no instance references at all (account-wide op)
        if extracted_ids and instance_id not in extracted_ids:
            continue

        # Also check the raw text as a fallback for unusual parameter layouts
        if extracted_ids == set() and instance_id in raw_data_str:
            # raw_data contains the instance ID as a string even if structured
            # extraction found nothing — include it
            pass

        ua = row.get("user_agent") or ""
        operation = row.get("operation") or ""
        is_read_only = operation.startswith(("Describe", "Get", "List"))

        cloudtrail_events.append({
            "time": str(row["time"]),
            "event_type": "cloudtrail",
            "operation": operation,
            "who": row.get("who") or "unknown",
            "user_type": row.get("user_type") or "",
            "source_ip": row.get("source_ip") or "",
            "user_agent": ua,
            "source": classify_source(ua),
            "status": row.get("status") or "unknown",
            "status_detail": row.get("status_detail") or "",
            "is_failure": row.get("status") == "Failure",
            "is_read_only": is_read_only,
            "detail": "",  # requestParameters parsing can be added later
        })

    # ------------------------------------------------------------------
    # Query 2: Config CDC — resource changes for this EC2 instance
    # ------------------------------------------------------------------
    config_events = []

    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT capture_time,
                       change_type,
                       status,
                       configuration,
                       tags,
                       resource_creation_time
                FROM {CATALOG}.silver.config_cdc
                WHERE resource_type LIKE 'AWS::EC2::%%'
                  AND resource_id = %(instance_id)s
                  AND capture_time BETWEEN %(start)s AND %(end)s
                ORDER BY capture_time ASC
            """, params)
            cols = [desc[0] for desc in cur.description]
            cfg_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        if "TABLE_OR_VIEW_NOT_FOUND" not in str(exc):
            pass
        cfg_rows = []

    for row in cfg_rows:
        change_type = row.get("change_type") or "Unknown"
        # Truncate configuration blob for display; full data lives in the table
        config_snippet = ""
        if row.get("configuration"):
            config_snippet = row["configuration"][:500]

        config_events.append({
            "time": str(row["capture_time"]),
            "event_type": "config",
            "operation": f"Config {change_type}",
            "who": "AWS Config",
            "user_type": "Service",
            "source_ip": "",
            "user_agent": "",
            "source": {"source_type": "config", "label": "Config", "color": "#f1fa8c"},
            "change_type": change_type,
            "status": "Success",
            "status_detail": "",
            "is_failure": False,
            "is_read_only": False,
            "detail": config_snippet,
        })

    # ------------------------------------------------------------------
    # Merge and sort by time (ISO string sort is lexicographic — correct
    # for timestamps as long as they are zero-padded, which Databricks ensures)
    # ------------------------------------------------------------------
    merged = cloudtrail_events + config_events
    merged.sort(key=lambda e: e["time"])
    return merged


# ---------------------------------------------------------------------------
# Timeline post-processing
# ---------------------------------------------------------------------------

def collapse_service_polls(timeline):
    """
    Collapse consecutive runs of read-only AWS service poll events into a
    single summary entry to reduce visual noise.

    A "service poll" is any event where:
      - source.source_type == "service"
      - is_read_only == True

    Consecutive runs of such events are replaced by a single dict with
    event_type="service_poll_group" that records the time range, count,
    and the original events list (for expand-on-click in the UI).

    Non-service events and write/mutating service events pass through
    unchanged.

    Returns a new list (does not mutate the input).
    """
    result = []
    i = 0

    while i < len(timeline):
        event = timeline[i]

        # Check if this event is a collapsible service poll
        is_service_poll = (
            event.get("source", {}).get("source_type") == "service"
            and event.get("is_read_only", False)
        )

        if not is_service_poll:
            result.append(event)
            i += 1
            continue

        # Accumulate consecutive service poll events
        run = [event]
        j = i + 1
        while j < len(timeline):
            next_event = timeline[j]
            next_is_service_poll = (
                next_event.get("source", {}).get("source_type") == "service"
                and next_event.get("is_read_only", False)
            )
            if next_is_service_poll:
                run.append(next_event)
                j += 1
            else:
                break

        if len(run) == 1:
            # Single isolated service poll — pass through without wrapping
            result.append(run[0])
        else:
            # Replace the run with a summary group entry
            result.append({
                "time": run[0]["time"],
                "time_end": run[-1]["time"],
                "event_type": "service_poll_group",
                "operation": f"{len(run)} AWS service polls",
                "who": "AWS Services",
                "source": {"source_type": "service", "label": "Service", "color": "#6272a4"},
                "count": len(run),
                "is_read_only": True,
                "is_failure": False,
                "events": run,  # preserve originals for expand-on-click
            })

        i = j

    return result
