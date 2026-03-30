# Security Investigation Capabilities

This project includes interactive investigation tools and automated detection pipelines built on the security data lakehouse. Each capability is described below with its data sources and key features.

---

## Host Investigation Graph

An interactive network graph for host-centric security triage, built as a Streamlit Databricks App.

**App:** `apps/investigation-graph/app.py`

![Host Investigation Graph](images/investigation-graph.png)

### Features

- **Host selector + time range picker** — choose a host and investigation window from the sidebar
- **Vis.js network graph** — hierarchical left-to-right layout: host → users → external connections
- **Node types:**
  - Host — green box (primary investigation target)
  - User — purple dot (OS users active on the host)
  - Auth IP — red dot (external IPs that authenticated)
  - SSH target — pink dot (hosts reached via SSH from the investigated host)
- **Edge types:** authentication events, process execution, network connections (management ports), SSH lateral movement
- **Category filtering** — client-side toggles for Auth, Commands, Account Changes, Network, and System Events. No page reload required.
- **Per-user toggles** — show/hide individual users to focus the graph
- **Collapsible command panels** — click a user or SSH target node to see the commands they executed, with SSH commands highlighted
- **SSH session correlation** — when the investigated host opens an SSH session to another host, the graph shows the remote host's commands during that session window
- **Stats row** — total events, unique users, and counts per event category
- **All Events table** — expandable data table with the full timeline, filtered by the same category/user selections

### Data Sources

| Table | Purpose |
|-------|---------|
| `silver.host_authentications` | Login/logout events, SSH sessions, privilege escalation |
| `silver.host_process_executions` | Commands executed by each user |
| `silver.host_account_changes` | Account creation/modification events |
| `silver.host_system_events` | Syscalls, service events, system-level activity |
| `gold.ec2_inventory` | IP → hostname mapping for graph annotation |

---

## EC2 Config Timeline

A vertical timeline showing every API call and configuration change for a specific EC2 instance, built as a Streamlit page within the investigation app.

**App:** `apps/investigation-graph/pages/ec2_timeline.py`

![EC2 Config Timeline](images/ec2-config-timeline.png)

### Features

- **Instance selector** — dropdown populated from `gold.ec2_inventory`, showing instance name and ID
- **Instance header card** — name, state, instance type, private IP, account ID, with expandable tags and security groups
- **Vertical timeline** — each event rendered as a card along a central timeline line, click to expand details
- **Source classification** — CloudTrail events are classified by `user_agent` into origin categories:

  | Source | Badge Color | Pattern |
  |--------|-------------|---------|
  | IaC (Terraform) | Blue | User agent contains `Terraform` |
  | Console | Orange | User agent contains `console.aws.amazon.com` |
  | CLI | Green | User agent contains `aws-cli` |
  | Script/SDK | Purple | User agent contains `boto3` or `aws-sdk` |
  | AWS Service | Gray | User agent ends with `.amazonaws.com` |

- **Config CDC events** — AWS Config configuration changes displayed with change-type pills: INSERT (green), UPDATE (yellow), DELETE (red)
- **Service poll collapsing** — consecutive read-only Describe/Get/List calls from AWS services are automatically grouped into a single summary entry (expandable)
- **Client-side filtering** — source type checkboxes, read-only operations toggle, sort direction (newest/oldest first)

### Data Sources

| Table | Purpose |
|-------|---------|
| `bronze.cloudtrail` | EC2 API calls (filtered to `ec2.amazonaws.com`) |
| `silver.config_cdc` | AWS Config CDC rows for EC2 resource changes |
| `gold.ec2_inventory` | Instance metadata for the header card and selector |

---

## Threat Intel Alert Pipeline

Automated matching of network flows against threat intelligence feeds, with alert forwarding to SNS.

### Features

- **Three TI feeds ingested daily:**
  - Feodo Tracker — known C2 server IPs (abuse.ch)
  - Emerging Threats compromised IPs (Proofpoint)
  - IPsum aggregated blocklist (score >= 3)
- **Silver layer** — IOCs deduplicated via MERGE with 90-day TTL expiration, keeping the network IOC table current without unbounded growth
- **Gold layer** — incremental watermark join: VPC Flow Logs (AWS, Azure, GCP) joined against IOCs on destination IP. Matches become alerts via MERGE on `alert_id`.
- **SNS forwarding** — new alerts read via Delta Change Data Feed and published to SNS. End-to-end latency from network event to SNS notification is ~10 minutes.
- **Cross-cloud** — OCSF normalization enables a single gold alerts notebook to process flows from all three clouds (AWS VPC Flow, Azure VNet Flow, GCP VPC Flow) without branching logic

### Data Flow

```
TI feeds (HTTP) → bronze.threat_intel_raw → silver.threat_intel_network_iocs
                                                        ↓
VPC Flow Logs (S3/ADLS/GCS) → bronze.vpc_flow ──→ gold.alerts → SNS
```

See [docs/threat-intel-alert-pipeline.md](threat-intel-alert-pipeline.md) for the full pipeline architecture and CDF redesign rationale.

---

## Investigation Notebooks

Gold-layer Databricks notebooks for on-demand investigation materialization and interactive graph rendering.

**Notebooks:**
- `notebooks/gold/04_gold_timeline_materialize.py` — timeline builder
- `notebooks/gold/05_investigation_graph.py` — vis.js graph renderer
- `notebooks/gold/00_timeline_common.py` — shared functions

### Features

- **Parameterized** — accepts `trigger_ip`, `hostname` (optional), `time_window_hours`, and `catalog_name`
- **Auto-discovery** — if hostname is not provided, discovers it from `silver.host_authentications` by matching the trigger IP
- **Identity chain discovery** — follows privilege escalation edges (sudo, su, runas) to map the full identity chain from the initial login through all user transitions
- **Relevance scoring** — each event scored 0–100 based on proximity to the trigger user/IP and position in the identity chain. Higher scores indicate events more likely related to the investigation.
- **Timeline materialization** — builds a unified timeline from all four silver host telemetry tables, applies relevance scoring, and MERGEs into `gold.user_activity_timeline` (deduped on `event_id`)
- **Graph rendering** — derives vis.js nodes and edges from the timeline and renders an interactive graph inline via `displayHTML()`

### Data Sources

All four `silver.host_*` tables feed into `gold.user_activity_timeline`.
