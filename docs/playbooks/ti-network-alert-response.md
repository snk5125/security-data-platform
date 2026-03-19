# Playbook: Threat Intel Network Alert Response

**Alert type:** `ti_network`
**Trigger:** SNS message from `security-lakehouse-alerts` topic, published by `03_gold_alerts_forward` notebook
**Pipeline:** `bronze-vpc-flow-ingest` job (Task 3: forward_alerts) — runs every 10 minutes

---

## Overview

A `ti_network` alert fires when a VPC Flow Log record from a monitored EC2 instance matches an active indicator in `silver.threat_intel_network`. The match means the instance communicated with an IP or CIDR that is currently listed in one of the threat intel feeds (Feodo Tracker, Emerging Threats, or IPsum).

This playbook covers: receiving the alert → initial triage → investigation → containment → closure.

---

## 1. Receiving the Alert

### SNS Message Structure

Each alert arrives as a JSON message with these fields populated for `ti_network` alerts:

```json
{
  "alert_id":         "<sha256-hex>",
  "alert_type":       "ti_network",
  "alert_class":      "detection_finding",
  "severity":         "Critical | High | Medium",
  "confidence_score": 3,
  "status":           "Active",
  "title":            "Threat Intel Network Hit: 185.220.101.22 (QakBot) [feodo_tracker]",

  "account_id":       "<WORKLOAD_A_ACCOUNT_ID>",
  "region":           "us-east-1",
  "instance_uid":     "<INSTANCE_ID>",

  "first_seen_at":    "2026-03-17T14:30:00+00:00",
  "last_seen_at":     "2026-03-17T14:39:59+00:00",
  "_computed_at":     "2026-03-17T14:42:11+00:00",

  "observable_type":  "ip",
  "observable_value": "185.220.101.22",
  "observable_id":    "<sha256-hex>",
  "threat_category":  "c2",
  "detection_source": "feodo_tracker",
  "detection_name":   "QakBot",

  "src_ip":           "10.0.1.45",
  "dst_ip":           "185.220.101.22",
  "dst_port":         2222,
  "flow_direction":   "egress",
  "action":           "Allowed",
  "vpc_uid":          "vpc-0abc1234",
  "matched_direction": "dst",
  "match_type":       "exact",

  "flow_count":       47,
  "total_bytes":      152300,
  "total_packets":    310,

  "details": {"ti_dest_port": "2222"}
}
```

**SNS message attributes** (usable in subscription filter policies):
- `severity` — `Critical`, `High`, or `Medium`
- `alert_type` — `ti_network`

### Severity / Confidence Mapping

| `confidence_score` | `severity` | Feed source | Interpretation |
|--------------------|------------|-------------|----------------|
| 3 | Critical | Feodo Tracker | Confirmed active C2 infrastructure (e.g., QakBot, Emotet, TrickBot) |
| 2 | High | Emerging Threats | Reputable blocklist or multiple-source corroboration |
| 1 | Medium | IPsum | Broad reputation list; benign false positives more likely |

---

## 2. Initial Triage (< 5 minutes)

Answer these four questions before escalating:

### 2a. Is the alert still active?

```sql
SELECT
  alert_id, severity, status, title,
  instance_uid, account_id, region,
  src_ip, dst_ip, dst_port,
  flow_direction, action,
  flow_count, total_bytes, total_packets,
  first_seen_at, last_seen_at,
  detection_source, detection_name, threat_category, confidence_score
FROM security_poc.gold.alerts
WHERE alert_id = '<alert_id_from_sns>'
```

If `status = 'Expired'`: the underlying IOC was removed from `silver.threat_intel_network` before you responded. Likely stale — proceed to [Section 6: Closure](#6-closure).

### 2b. How many flows? How recent?

Look at `flow_count` and `last_seen_at` from the query above.

- `flow_count = 1`, `last_seen_at` minutes ago: single connection, may be a scan or one-off.
- `flow_count > 10`, `last_seen_at` within the last hour: active, ongoing communication.
- `action = 'Denied'`: VPC security group blocked the traffic. Still worth investigating — the instance tried to reach C2. Lower urgency than `Allowed`.

### 2c. Is the IOC still in the threat intel database?

```sql
SELECT
  ioc_value, feed_name, threat_category, confidence_score,
  dest_port, malware_family,
  first_seen_at, last_seen_at, expires_at
FROM security_poc.silver.threat_intel_network
WHERE ioc_id = '<observable_id_from_alert>'
```

If the row is missing: the IOC expired and was deleted from silver. The alert was generated against an indicator that is no longer active — lower urgency.

### 2d. What is the instance?

```sql
SELECT
  instance_id, account_id, region,
  instance_type, platform, state,
  private_ip_addresses,
  public_ip_address,
  security_groups,
  vpc_id, subnet_id,
  _capture_time
FROM security_poc.gold.ec2_inventory
WHERE instance_id = '<instance_uid_from_alert>'
```

Note: `_capture_time` is the last time Config recorded a state change — not real-time. Use this for context (instance type, SGs, who owns it) rather than current-state confirmation.

---

## 3. Investigation

### 3a. How long has this instance been talking to this IOC?

```sql
SELECT
  DATE_TRUNC('hour', time) AS hour,
  COUNT(*) AS flow_count,
  SUM(traffic.bytes) AS bytes,
  SUM(traffic.packets) AS packets,
  COLLECT_SET(src_endpoint.port) AS src_ports,
  COLLECT_SET(dst_endpoint.port) AS dst_ports,
  FIRST(action) AS action
FROM security_poc.bronze.vpc_flow
WHERE src_endpoint.instance_uid = '<instance_uid>'
  AND dst_endpoint.ip = '<observable_value>'
ORDER BY hour DESC
LIMIT 48
```

**What to look for:**
- Repeating connections on the same dst_port at regular intervals → likely C2 beacon
- Large total_bytes in egress direction → possible data exfiltration
- Many distinct src_ports, low bytes → scanning behavior

### 3b. Has this instance talked to any other threat intel IOCs?

```sql
SELECT
  observable_value, detection_source, detection_name,
  threat_category, severity,
  flow_count, total_bytes, first_seen_at, last_seen_at,
  matched_direction
FROM security_poc.gold.alerts
WHERE instance_uid = '<instance_uid>'
  AND alert_type = 'ti_network'
ORDER BY last_seen_at DESC
```

Multiple IOC hits on the same instance — especially from `feodo_tracker` or multiple threat categories — is a strong indicator of compromise.

### 3c. Have any other instances contacted this IOC?

```sql
SELECT
  instance_uid, account_id, region,
  flow_count, total_bytes,
  first_seen_at, last_seen_at
FROM security_poc.gold.alerts
WHERE observable_value = '<ioc_value>'
  AND alert_type = 'ti_network'
ORDER BY last_seen_at DESC
```

Lateral spread: multiple instances hitting the same C2 IP suggests a worm or lateral movement.

### 3d. What did the VPC Flow look like just before and after?

```sql
SELECT
  time,
  src_endpoint.ip AS src_ip,
  src_endpoint.port AS src_port,
  src_endpoint.instance_uid AS src_instance,
  dst_endpoint.ip AS dst_ip,
  dst_endpoint.port AS dst_port,
  connection_info.direction_id,
  action,
  traffic.bytes,
  traffic.packets
FROM security_poc.bronze.vpc_flow
WHERE src_endpoint.instance_uid = '<instance_uid>'
  AND time BETWEEN TIMESTAMPADD(HOUR, -1, '<first_seen_at>') AND TIMESTAMPADD(HOUR, 1, '<last_seen_at>')
ORDER BY time ASC
```

Look for unusual destination IPs, ports, or byte volumes around the time of the IOC contact.

### 3e. Full threat intel context for this IOC

```sql
SELECT *
FROM security_poc.silver.threat_intel_network
WHERE ioc_value = '<observable_value>'
```

For Feodo indicators: check `malware_family`, `dest_port`. The `dest_port` in the alert's `details.ti_dest_port` is the port Feodo expects C2 traffic on — if `dst_port` in the flow matches, confidence is very high.

---

## 4. Escalation Criteria

Escalate to incident response if ANY of the following are true:

| Condition | Why |
|-----------|-----|
| `severity = Critical` AND `action = Allowed` | Confirmed C2 contact with a Feodo-tracked host — no network block |
| `flow_count > 5` AND `flow_direction = egress` | Repeated outbound connections to known-bad IP |
| `total_bytes > 100 KB` AND `matched_direction = dst` (egress) | Significant data volume toward a threat intel IP |
| Multiple distinct IOC hits on same instance | Strong indicator of active infection |
| Multiple instances hitting same IOC | Potential lateral spread |
| `detection_name` is a specific malware family (e.g., QakBot, Emotet, TrickBot, Cobalt Strike) AND `action = Allowed` | Confirmed C2 family contact |

Do not escalate if:
- `action = Denied` AND `flow_count = 1` AND `confidence_score = 1` (IPsum list) — likely blocked scan
- `match_type = cidr` AND `confidence_score = 1` — broad IP reputation range, high false positive rate

---

## 5. Containment

### Immediate (while investigating)

**Option A — Restrict egress via Security Group (preferred, reversible)**

Find the instance's security group from `ec2_inventory.security_groups`. Remove or restrict the outbound rule permitting traffic to the IOC IP. This does not terminate running processes but stops further C2 communication.

```bash
# Example: remove allow-all egress rule from the instance's SG
aws ec2 revoke-security-group-egress \
  --group-id sg-xxxxxxxx \
  --protocol all \
  --cidr 0.0.0.0/0 \
  --region us-east-1 \
  --profile workload_a
```

**Option B — Stop the instance (if escalation confirms compromise)**

```bash
aws ec2 stop-instances \
  --instance-ids <INSTANCE_ID> \
  --region us-east-1 \
  --profile workload_a
```

**Option C — Isolate with dedicated deny SG (forensic preservation)**

Attach an SG with no inbound or outbound rules. This preserves the instance state for forensic analysis while preventing further network communication.

### Threat intel feed context

- **Feodo Tracker** IPs are updated daily. If an IP was delisted before you act, the alert's `observable_id` will no longer appear in `silver.threat_intel_network`. Check via [Section 2c](#2c-is-the-ioc-still-in-the-threat-intel-database).
- **Emerging Threats** and **IPsum** IPs rotate more frequently. An IPsum hit on `confidence_score = 1` may be a false positive — validate before containment.

---

## 6. Closure

Update the alert status in `gold.alerts` after resolution. The `status` column is not managed automatically — it is set by analyst workflow.

```sql
-- After confirming true positive and containment:
UPDATE security_poc.gold.alerts
SET status = 'Resolved'
WHERE alert_id = '<alert_id>';

-- After confirming false positive:
UPDATE security_poc.gold.alerts
SET status = 'Suppressed'
WHERE alert_id = '<alert_id>';

-- Suppress all future alerts for this IOC + instance pair
-- (same alert_id will be regenerated on the next daily run if the IOC is still active):
-- Option: add the IOC to a suppression list table (not yet implemented)
-- Option: set status = 'Suppressed' on each regenerated alert_id
```

**Document your findings.** At minimum, record:
- Whether the alert was a true positive or false positive
- What evidence supported the determination
- What containment action was taken (if any)
- Whether the IOC should be added to a permanent blocklist

---

## 7. Reference: Key Tables

| Table | Purpose |
|-------|---------|
| `security_poc.gold.alerts` | Alert rows — one per (instance, IOC, feed) pairing |
| `security_poc.gold.alerts_forwarded` | SNS delivery log — links `alert_id` to `sns_message_id` |
| `security_poc.silver.threat_intel_network` | Active IOCs — query here for feed context and expiry |
| `security_poc.bronze.vpc_flow` | Raw VPC Flow Logs in OCSF format — use for timeline reconstruction |
| `security_poc.gold.ec2_inventory` | Current-state EC2 inventory — instance metadata, SGs, VPC |

---

## 8. Reference: Detection Sources

| `detection_source` | Feed | `threat_category` | Notes |
|--------------------|------|-------------------|-------|
| `feodo_tracker` | Feodo Tracker | `c2` | Botnet C2 IPs (QakBot, Emotet, TrickBot, etc.) — most actionable |
| `emerging_threats` | Emerging Threats | `compromised` | IPs associated with known-bad actors — reputable list |
| `ipsum` | IPsum | `reputation` | Aggregate IP reputation list — more false positives, use as corroboration |
