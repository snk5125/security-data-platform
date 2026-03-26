# -----------------------------------------------------------------------------
# Investigation Graph Backend — SQL-based timeline and graph logic
# -----------------------------------------------------------------------------
# Host-centric investigation: query all silver tables for a hostname or
# pattern, build a unified timeline, and derive a vis.js graph.
# -----------------------------------------------------------------------------

import hashlib
import json
import re
from datetime import datetime, timedelta

CATALOG = "security_poc"

# Management ports to include in network activity queries
MGMT_PORTS = (22, 3389, 443, 80)
MGMT_PORT_LABELS = {22: "SSH", 3389: "RDP", 443: "HTTPS", 80: "HTTP"}


def get_connection(cfg, warehouse_id):
    """Create a Databricks SQL connection."""
    from databricks import sql
    return sql.connect(
        server_hostname=cfg.host,
        http_path=f"/sql/1.0/warehouses/{warehouse_id}",
        credentials_provider=lambda: cfg.authenticate,
        use_cloud_fetch=False,
    )


def list_hosts(conn):
    """Return distinct hostnames across all silver tables."""
    hosts = set()
    for table in ["host_authentications", "host_process_executions",
                  "host_account_changes", "host_system_events"]:
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT DISTINCT hostname FROM {CATALOG}.silver.{table}")
                for row in cur.fetchall():
                    if row[0]:
                        hosts.add(row[0])
        except Exception:
            pass
    return sorted(hosts)


def _query_table(conn, table, hostname_pattern, start_time, end_time):
    """Query a silver table filtered by hostname pattern and time window."""
    # Support LIKE patterns (% wildcard) or exact match
    use_like = "%" in hostname_pattern or "_" in hostname_pattern
    where = "hostname LIKE %(host)s" if use_like else "hostname = %(host)s"
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT * FROM {CATALOG}.silver.{table}
            WHERE {where}
              AND time BETWEEN %(start)s AND %(end)s
            ORDER BY time ASC
        """, {"host": hostname_pattern, "start": str(start_time), "end": str(end_time)})
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def build_ip_hostname_map(conn):
    """
    Build a reverse lookup of IP → hostname from ec2_inventory (both private
    and public IPs).  Used to annotate remote IPs in the graph with known
    hostnames (e.g., label <PUBLIC_IP> as "ip-10-0-1-14.ec2.internal").
    """
    ip_map = {}
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT private_ip, public_ip, private_dns_name, tags
                FROM {CATALOG}.gold.ec2_inventory
                WHERE private_ip IS NOT NULL
            """)
            for row in cur.fetchall():
                private_ip, public_ip, dns_name, tags_str = row[0], row[1], row[2], row[3]
                label = dns_name.split(".")[0] if dns_name else private_ip
                if private_ip:
                    ip_map[private_ip] = label
                if public_ip:
                    ip_map[public_ip] = label
    except Exception:
        pass
    return ip_map


def resolve_host_ips(conn, hostname):
    """
    Resolve a hostname to its private IP(s) by combining:
      1. ec2_inventory — match Name tag or private_dns_name against hostname
      2. Azure/GCP — match hostname conventions to known internal IPs from flow logs
    Returns a list of IP strings (may be empty if resolution fails).
    """
    ips = set()

    # Strategy 1: AWS ec2_inventory — Name tag contains the Terraform resource name
    # which often partially matches the hostname (e.g., "lakehouse-workload-a-linux").
    # Also check private_dns_name for Linux hosts (ip-10-0-1-14.ec2.internal).
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT private_ip, public_ip, private_dns_name, tags
                FROM {CATALOG}.gold.ec2_inventory
                WHERE private_ip IS NOT NULL
            """)
            for row in cur.fetchall():
                private_ip, public_ip, dns_name, tags_str = row[0], row[1], row[2], row[3]
                try:
                    tags = json.loads(tags_str) if tags_str else {}
                except (json.JSONDecodeError, TypeError):
                    tags = {}
                name_tag = tags.get("Name", "")
                matched = False
                if hostname.lower() in name_tag.lower() or name_tag.lower() in hostname.lower():
                    matched = True
                if dns_name and hostname.lower() in dns_name.lower():
                    matched = True
                if matched:
                    ips.add(private_ip)
                    # Include public IP so cross-cloud connections (e.g. SSH from
                    # AWS to Azure via public IP) are captured in flow log queries.
                    if public_ip:
                        ips.add(public_ip)
    except Exception:
        pass

    # Strategy 2: For Windows AWS hosts (EC2AMAZ-*), we can't match by name tag.
    # Use instance_uid from VPC flow logs cross-referenced with ec2_inventory.
    # This is a heuristic: if there's exactly one unmatched Windows instance in
    # the same account, it's likely the match.  For now, fall through to Strategy 3.

    # Strategy 3: Azure/GCP — query distinct internal IPs from flow logs.
    # Azure VMs use hostnames like "lakehouse-azure-workload-a-vm-linux" / "lakehouseazuwin".
    # GCP VMs use "lakehouse-gcp-workload-a-linux" / "lakehouse-gcp-workload-a-windows".
    # Match cloud keyword in hostname to the corresponding flow table's internal IPs.
    if not ips:
        cloud_table_map = {
            "azure": "vnet_flow_raw",
            "gcp": "gcp_vpc_flow_raw",
        }
        for cloud_key, table in cloud_table_map.items():
            if cloud_key in hostname.lower():
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"""
                            SELECT DISTINCT src_endpoint.ip
                            FROM {CATALOG}.bronze.{table}
                            WHERE src_endpoint.ip LIKE '10.%%'
                        """)
                        cloud_ips = sorted([row[0] for row in cur.fetchall() if row[0]])
                except Exception:
                    cloud_ips = []
                # Heuristic: lower IP = Linux, higher IP = Windows within each cloud
                is_windows = "win" in hostname.lower()
                # Filter out gateway IPs (typically .1)
                vm_ips = [ip for ip in cloud_ips if not ip.endswith(".1")]
                if vm_ips:
                    if is_windows:
                        ips.add(vm_ips[-1])  # higher IP
                    else:
                        ips.add(vm_ips[0])   # lower IP
                break

    return list(ips)


def _query_network_activity(conn, host_ips, start_time, end_time):
    """
    Query VPC/VNet/GCP flow logs for management port connections (SSH, RDP,
    HTTPS, HTTP) involving the given host IPs.

    Returns two lists: (allowed_rows, denied_summary).
      - allowed_rows: one row per (src, dst, port) for Allowed connections
      - denied_summary: one row per (direction, port) summarising all denied
        attempts with unique IP count, so the graph shows a single node
    """
    if not host_ips:
        return [], []

    ip_placeholders = ", ".join(f"'{ip}'" for ip in host_ips)
    ports_clause = ", ".join(str(p) for p in MGMT_PORTS)
    ip_set = set(host_ips)

    flow_tables = ["vpc_flow", "vnet_flow_raw", "gcp_vpc_flow_raw"]
    allowed_rows = []
    denied_rows = []

    for table in flow_tables:
        params = {"start": str(start_time), "end": str(end_time)}
        base_where = (
            f"(src_endpoint.ip IN ({ip_placeholders}) "
            f"OR dst_endpoint.ip IN ({ip_placeholders})) "
            f"AND dst_endpoint.port IN ({ports_clause}) "
            f"AND time BETWEEN %(start)s AND %(end)s"
        )

        # Allowed connections — full detail per (src, dst, port)
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        MIN(time) AS time,
                        src_endpoint.ip AS src_ip,
                        dst_endpoint.ip AS dst_ip,
                        dst_endpoint.port AS dst_port,
                        'Allowed' AS action,
                        SUM(traffic.bytes) AS bytes,
                        SUM(traffic.packets) AS packets,
                        COUNT(*) AS flow_count
                    FROM {CATALOG}.bronze.{table}
                    WHERE {base_where}
                      AND LOWER(action) = 'allowed'
                    GROUP BY src_endpoint.ip, dst_endpoint.ip,
                             dst_endpoint.port
                    ORDER BY time ASC
                    LIMIT 200
                """, params)
                cols = [desc[0] for desc in cur.description]
                allowed_rows.extend(dict(zip(cols, r)) for r in cur.fetchall())
        except Exception as e:
            if "TABLE_OR_VIEW_NOT_FOUND" not in str(e):
                raise

        # Denied — summarise per port only (collapse all remote IPs)
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        MIN(time) AS time,
                        dst_endpoint.port AS dst_port,
                        COUNT(*) AS flow_count,
                        COUNT(DISTINCT src_endpoint.ip) AS unique_src_ips,
                        COUNT(DISTINCT dst_endpoint.ip) AS unique_dst_ips,
                        SUM(traffic.bytes) AS bytes
                    FROM {CATALOG}.bronze.{table}
                    WHERE {base_where}
                      AND LOWER(action) != 'allowed'
                    GROUP BY dst_endpoint.port
                    LIMIT 20
                """, params)
                cols = [desc[0] for desc in cur.description]
                denied_rows.extend(dict(zip(cols, r)) for r in cur.fetchall())
        except Exception as e:
            if "TABLE_OR_VIEW_NOT_FOUND" not in str(e):
                raise

    return allowed_rows, denied_rows


def build_host_timeline(conn, hostname_pattern, start_time, end_time):
    """
    Build a unified activity timeline for a host (or pattern).
    Returns (timeline, stats) where stats has event counts by category.
    """
    timeline = []

    # Auth
    try:
        rows = _query_table(conn, "host_authentications", hostname_pattern, start_time, end_time)
        for r in rows:
            r["event_category"] = "authentication"
            r["detail"] = f"{r.get('auth_method', '')} from {r.get('source_ip', '')}".strip()
            r["source_host"] = r.get("hostname", "")
        timeline.extend(rows)
    except Exception as e:
        if "TABLE_OR_VIEW_NOT_FOUND" not in str(e):
            raise

    # Process executions
    try:
        rows = _query_table(conn, "host_process_executions", hostname_pattern, start_time, end_time)
        for r in rows:
            cmd = r.get("command_line", "")
            # Filter out bash HISTTIMEFORMAT epoch lines (e.g., "#1774387994")
            if cmd and cmd.startswith("#") and cmd[1:].strip().isdigit():
                continue
            r["event_category"] = "process_execution"
            r["action"] = "executed"
            r["detail"] = cmd
            r["source_host"] = r.get("hostname", "")
        timeline.extend([r for r in rows if "event_category" in r])
    except Exception as e:
        if "TABLE_OR_VIEW_NOT_FOUND" not in str(e):
            raise

    # Account changes
    try:
        rows = _query_table(conn, "host_account_changes", hostname_pattern, start_time, end_time)
        for r in rows:
            r["event_category"] = "account_change"
            r["user"] = r.get("acting_user", r.get("user", ""))
            r["detail"] = f"{r.get('action', '')} {r.get('target_user', '')}".strip()
            r["source_host"] = r.get("hostname", "")
        timeline.extend(rows)
    except Exception as e:
        if "TABLE_OR_VIEW_NOT_FOUND" not in str(e):
            raise

    # System events
    try:
        rows = _query_table(conn, "host_system_events", hostname_pattern, start_time, end_time)
        for r in rows:
            r["event_category"] = "system_event"
            r["action"] = "system"
            r["detail"] = r.get("message", "")
            r["source_host"] = r.get("hostname", "")
        timeline.extend(rows)
    except Exception as e:
        if "TABLE_OR_VIEW_NOT_FOUND" not in str(e):
            raise

    # Network activity (VPC/VNet/GCP flow logs on management ports)
    try:
        host_ips = resolve_host_ips(conn, hostname_pattern)
        host_ip_set = set(host_ips)
        allowed_rows, denied_rows = _query_network_activity(
            conn, host_ips, start_time, end_time)

        # Allowed connections — one timeline entry per (src, dst, port)
        for r in allowed_rows:
            src_ip = r.get("src_ip", "")
            dst_ip = r.get("dst_ip", "")
            dst_port = r.get("dst_port", 0)
            port_label = MGMT_PORT_LABELS.get(dst_port, str(dst_port))
            if src_ip in host_ip_set:
                direction = "outbound"
                remote_ip = dst_ip
            else:
                direction = "inbound"
                remote_ip = src_ip
            flow_count = r.get("flow_count", 1)
            total_bytes = r.get("bytes", 0) or 0
            r["event_category"] = "network_connection"
            r["action"] = "Allowed"
            r["user"] = ""
            r["source_ip"] = remote_ip
            r["source_host"] = hostname_pattern
            r["hostname"] = hostname_pattern
            r["detail"] = (f"{direction} {port_label} ({dst_port}) "
                           f"{src_ip} → {dst_ip} "
                           f"[Allowed] {flow_count} flows, {total_bytes} bytes")
            r["direction"] = direction
            r["dst_port"] = dst_port
            r["port_label"] = port_label
            r["remote_ip"] = remote_ip
            r["is_denied"] = False
        timeline.extend(allowed_rows)

        # Denied — one summary entry per port (collapsed)
        for r in denied_rows:
            dst_port = r.get("dst_port", 0)
            port_label = MGMT_PORT_LABELS.get(dst_port, str(dst_port))
            flow_count = r.get("flow_count", 0)
            unique_ips = r.get("unique_src_ips", 0)
            total_bytes = r.get("bytes", 0) or 0
            r["event_category"] = "network_connection"
            r["action"] = "Denied"
            r["user"] = ""
            r["source_ip"] = ""
            r["source_host"] = hostname_pattern
            r["hostname"] = hostname_pattern
            r["detail"] = (f"Denied {port_label} ({dst_port}): "
                           f"{flow_count} attempts from {unique_ips} IPs, "
                           f"{total_bytes} bytes")
            r["direction"] = "inbound"
            r["dst_port"] = dst_port
            r["port_label"] = port_label
            r["remote_ip"] = ""
            r["is_denied"] = True
        timeline.extend(denied_rows)
    except Exception as e:
        if "TABLE_OR_VIEW_NOT_FOUND" not in str(e):
            raise

    timeline.sort(key=lambda r: r.get("time") or datetime.min)
    return timeline


def _parse_ssh_target(cmd):
    """Parse [user, host] from an ssh command line, skipping flags and their args."""
    if not cmd or not cmd.startswith("ssh "):
        return None, None
    tokens = cmd.split()
    # SSH flags that consume the next token as their argument
    flags_with_arg = {
        "-i", "-o", "-p", "-l", "-F", "-J", "-W", "-b", "-c",
        "-D", "-E", "-L", "-R", "-S", "-e", "-m", "-w",
    }
    i = 1  # skip "ssh"
    while i < len(tokens):
        tok = tokens[i]
        if tok in flags_with_arg:
            i += 2  # skip flag + its value
        elif tok.startswith("-"):
            i += 1  # skip boolean flag
        else:
            # First non-flag token is the destination: [user@]host
            if "@" in tok:
                user, host = tok.rsplit("@", 1)
                return user, host
            return None, tok
    return None, None


def derive_graph(timeline, ip_hostname_map=None):
    """
    Convert timeline list to vis.js nodes and edges.

    Produces only structural nodes (host, user), authentication nodes (external
    IP sources, escalation edges), and network nodes (allowed connections, denied
    summaries).  Command, account-change, and system-event nodes are intentionally
    excluded — those are surfaced through build_command_index() instead, keeping
    the graph readable even with 100+ commands.

    ip_hostname_map: dict of {ip: hostname_label} for annotating known IPs.
    Returns (nodes_list, edges_list).
    """
    ip_hostname_map = ip_hostname_map or {}
    nodes = {}
    edges = []

    # edge_counter provides stable, unique IDs for every edge so vis.js click
    # handlers can look up edge metadata via allEdges.find(e => e.id === ...).
    edge_counter = [0]

    def next_edge_id(prefix):
        edge_counter[0] += 1
        return f"edge:{prefix}:{edge_counter[0]}"

    # Per-user counts for node sizing and label summaries.
    # All event categories (including commands/account/system) contribute to the
    # total so user node size reflects true activity volume.
    user_event_counts = {}
    user_cmd_counts = {}    # process_execution events
    user_acct_counts = {}   # account_change events

    for row in timeline:
        cat = row.get("event_category", "")
        user = row.get("user") or "unknown"
        action = row.get("action", "")
        detail = row.get("detail", "")
        source_ip = row.get("source_ip", "")
        time_str = str(row.get("time", ""))
        hostname = row.get("source_host", row.get("hostname", ""))

        # Host node — structural anchor for this investigation
        host_id = f"host:{hostname}"
        if hostname and host_id not in nodes:
            nodes[host_id] = {
                "id": host_id, "label": hostname,
                "type": "host", "color": "#50fa7b",
                "shape": "box", "size": 25,
                "font": {"color": "#1a1a2e"},
                "level": 1,
                "event_category": "structural",
            }

        # User node (scoped to host so the same username on two hosts is distinct).
        # Network events have no real user — skip user node to avoid "unknown" clutter.
        has_real_user = user != "unknown"
        user_node_id = f"user:{user}@{hostname}" if hostname else f"user:{user}"

        if has_real_user and user_node_id not in nodes:
            nodes[user_node_id] = {
                "id": user_node_id, "label": user,
                "type": "user", "color": "#bd93f9",
                "shape": "dot", "size": 15,
                "font": {"color": "white"},
                "level": 2,
                "event_category": "structural",
            }
            # Connect user to host with a structural (low-opacity) edge
            edges.append({
                "id": next_edge_id("host-user"),
                "from": host_id, "to": user_node_id,
                "color": {"color": "#50fa7b", "opacity": 0.4}, "width": 1,
                "event_category": "structural",
            })

        # Count events for user sizing (skip if no real user)
        if has_real_user:
            user_event_counts[user_node_id] = user_event_counts.get(user_node_id, 0) + 1

        # Track command and account-change counts separately for label summaries
        if cat == "process_execution":
            user_cmd_counts[user_node_id] = user_cmd_counts.get(user_node_id, 0) + 1

            # Detect SSH commands and create lateral movement edges.
            # Parse target from commands like:
            #   ssh user@host, ssh -i key.pem user@host, ssh host
            cmd = detail or ""
            ssh_user, ssh_target = _parse_ssh_target(cmd)
            if ssh_target:
                # Create a target node (resolve hostname if possible)
                target_label = ip_hostname_map.get(ssh_target, "") if ip_hostname_map else ""
                target_node_label = f"{target_label}\n{ssh_target}" if target_label else ssh_target
                target_id = f"ssh_target:{ssh_target}"
                if target_id not in nodes:
                    nodes[target_id] = {
                        "id": target_id,
                        "label": target_node_label,
                        "type": "ssh_target",
                        "color": "#ff79c6",
                        "shape": "box", "size": 18,
                        "font": {"color": "#1a1a2e"},
                        "level": 3,
                        "title": f"SSH target: {ssh_user}@{ssh_target}" if ssh_user else f"SSH target: {ssh_target}",
                        "event_category": "process_execution",
                    }
                edges.append({
                    "id": next_edge_id("ssh"),
                    "from": host_id, "to": target_id,
                    "label": f"SSH → {ssh_user}@" if ssh_user else "SSH →",
                    "color": {"color": "#ff79c6"}, "width": 2,
                    "title": f"{time_str}: {cmd}",
                    "event_category": "process_execution",
                })

        elif cat == "account_change":
            user_acct_counts[user_node_id] = user_acct_counts.get(user_node_id, 0) + 1

        # --- Authentication branch ---
        # Adds external IP source nodes and escalation edges only; no command nodes.
        if cat == "authentication":
            if source_ip and not source_ip.startswith(("10.", "172.16.", "192.168.", "")):
                # External (non-RFC-1918) IP — show as a red source node
                ip_id = f"ip:{source_ip}"
                known_host = ip_hostname_map.get(source_ip, "")
                auth_ip_label = f"{known_host}\n{source_ip}" if known_host else source_ip
                if ip_id not in nodes:
                    nodes[ip_id] = {
                        "id": ip_id, "label": auth_ip_label,
                        "type": "external_ip", "color": "#ff5555",
                        "shape": "dot", "size": 20,
                        "font": {"color": "white"},
                        "level": 0,
                        "title": f"{source_ip} ({known_host})" if known_host else source_ip,
                        "event_category": "authentication",
                    }
                edges.append({
                    "id": next_edge_id("auth"),
                    "from": ip_id, "to": user_node_id,
                    "label": action, "color": {"color": "#ff5555"}, "width": 2,
                    "title": f"{time_str}: {action} as {user} from {source_ip}",
                    "event_category": "authentication",
                })
            elif action == "escalation":
                # Privilege escalation — dashed edge between user nodes
                src = detail.split("by ")[-1].strip() if "by " in detail else user
                src_id = f"user:{src}@{hostname}" if hostname else f"user:{src}"
                edges.append({
                    "id": next_edge_id("escalation"),
                    "from": src_id, "to": user_node_id,
                    "label": "escalated", "color": {"color": "#ff79c6"},
                    "dashes": True, "width": 2,
                    "title": f"{time_str}: {detail}",
                    "event_category": "authentication",
                })

        # --- Network connection branch ---
        # Denied attempts collapse to a single summary node per port.
        # Allowed connections show a remote-IP node + port node chain.
        elif cat == "network_connection":
            is_denied = row.get("is_denied", False)

            if is_denied:
                dst_port = row.get("dst_port", 0)
                port_label = row.get("port_label", str(dst_port))
                denied_id = f"denied:{dst_port}"
                if denied_id not in nodes:
                    nodes[denied_id] = {
                        "id": denied_id, "label": f"Denied {port_label}",
                        "type": "denied_summary",
                        "color": "#ff5555",
                        "shape": "diamond", "size": 14,
                        "font": {"color": "white", "size": 11},
                        "level": 0,
                        "title": detail,
                        "event_category": "network_connection",
                    }
                    edges.append({
                        "id": next_edge_id("denied"),
                        "from": denied_id, "to": host_id,
                        "label": f"✕ {port_label}",
                        "color": {"color": "#ff5555", "opacity": 0.6},
                        "width": 2, "dashes": True,
                        "title": detail,
                        "event_category": "network_connection",
                    })
            else:
                remote_ip = row.get("remote_ip", "")
                dst_port = row.get("dst_port", 0)
                port_label = row.get("port_label", str(dst_port))
                direction = row.get("direction", "")

                ip_id = f"netip:{remote_ip}"
                is_internal = remote_ip.startswith(("10.", "172.16.", "192.168."))
                known_host = ip_hostname_map.get(remote_ip, "")
                ip_label = f"{known_host}\n{remote_ip}" if known_host else remote_ip
                if ip_id not in nodes:
                    nodes[ip_id] = {
                        "id": ip_id, "label": ip_label,
                        "type": "network_ip",
                        "color": "#ffb86c" if is_internal else "#ff5555",
                        "shape": "dot", "size": 18,
                        "font": {"color": "white"},
                        "title": f"{remote_ip} ({known_host})" if known_host else remote_ip,
                        "event_category": "network_connection",
                    }

                port_node_id = f"port:{remote_ip}:{dst_port}"
                if port_node_id not in nodes:
                    nodes[port_node_id] = {
                        "id": port_node_id, "label": port_label,
                        "type": "network_port",
                        "color": "#8be9fd",
                        "shape": "box", "size": 10,
                        "font": {"color": "#1a1a2e", "size": 11},
                        "event_category": "network_connection",
                    }
                    edges.append({
                        "id": next_edge_id("netip-port"),
                        "from": ip_id, "to": port_node_id,
                        "color": {"color": "#8be9fd", "opacity": 0.5},
                        "width": 1,
                        "event_category": "network_connection",
                    })

                if direction == "outbound":
                    edge_from, edge_to = host_id, port_node_id
                    dir_label = f"→ {port_label}"
                else:
                    edge_from, edge_to = port_node_id, host_id
                    dir_label = f"← {port_label}"

                edges.append({
                    "id": next_edge_id("net-dir"),
                    "from": edge_from, "to": edge_to,
                    "label": dir_label,
                    "color": {"color": "#8be9fd"},
                    "width": 1,
                    "title": f"{time_str}: {detail}",
                    "event_category": "network_connection",
                })

    # Scale user node sizes by total event count (capped to keep layout readable)
    for key, count in user_event_counts.items():
        if key in nodes:
            nodes[key]["size"] = min(40, 15 + count)

    # Update user node labels with command/account-change summary counts so
    # analysts can see activity volume directly on the node without expanding
    # the command panel (e.g. "ec2-user\n(35 cmds, 2 acct)").
    for key, node in nodes.items():
        if node.get("type") != "user":
            continue
        base_label = node["label"]
        cmds = user_cmd_counts.get(key, 0)
        accts = user_acct_counts.get(key, 0)
        if cmds or accts:
            parts = []
            if cmds:
                parts.append(f"{cmds} cmds")
            if accts:
                parts.append(f"{accts} acct")
            node["label"] = f"{base_label}\n({', '.join(parts)})"

    return list(nodes.values()), edges


def _resolve_ssh_sessions(conn, timeline, host_ips, end_time):
    """
    Correlate SSH commands in the investigated host's process timeline with
    authentication and process events on the SSH target host.

    For each `ssh` command found in `timeline`:
      1. Look up the login event on the target host that corresponds to a
         connection arriving from one of the investigated host's IPs.
      2. Determine the session window (login → next logout/disconnect, or end_time).
      3. Query host_process_executions on the target host for that window,
         filtered to the SSH user.
      4. Build a command list in the same shape as build_command_index entries.

    Returns a dict keyed by "ssh_target:{ip}" with shape:
        {
          "ssh_target:10.0.2.55": {
              "label": "ec2-user@ip-10-0-2-55 (SSH session)",
              "commands": [{"time": "...", "command_line": "...", "is_ssh": bool}, ...]
          }
        }

    Wrapped entirely in try/except — returns {} on any failure so a missing
    target host or absent data never breaks the investigation app.
    """
    if not host_ips:
        return {}

    # Build comma-separated quoted IP list for SQL IN clauses (same pattern as
    # _query_network_activity — parameterized bind vars don't work for IN lists
    # in the Databricks SQL connector).
    ip_placeholders = ", ".join(f"'{ip}'" for ip in host_ips)

    result = {}
    seen_targets = set()  # deduplicate — only resolve each target IP once

    try:
        for row in timeline:
            if row.get("event_category") != "process_execution":
                continue

            cmd = row.get("detail", "") or ""
            ssh_user, ssh_target_ip = _parse_ssh_target(cmd)
            if not ssh_target_ip:
                continue

            target_key = f"ssh_target:{ssh_target_ip}"
            if target_key in seen_targets:
                continue
            seen_targets.add(target_key)

            ssh_time = row.get("time")
            if ssh_time is None:
                continue

            # Convert to datetime if it came back as a string
            if isinstance(ssh_time, str):
                try:
                    ssh_time = datetime.fromisoformat(ssh_time)
                except ValueError:
                    continue

            window_end = ssh_time + timedelta(seconds=120)

            # Step 1: Find the login event on the target host that was initiated
            # from one of the investigated host's IPs.  Bash history timestamps
            # reflect when the command was written to the file (often after the
            # session ended), not when it was executed.  So we search backwards:
            # find the most recent login BEFORE the recorded command time.
            try:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT hostname, user, time
                        FROM {CATALOG}.silver.host_authentications
                        WHERE source_ip IN ({ip_placeholders})
                          AND action = 'login'
                          AND time <= %(ssh_time)s
                        ORDER BY time DESC
                        LIMIT 1
                    """, {
                        "ssh_time": str(ssh_time),
                    })
                    cols = [desc[0] for desc in cur.description]
                    login_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception:
                continue

            if not login_rows:
                continue

            login = login_rows[0]
            target_hostname = login.get("hostname", "")
            session_user = login.get("user") or ssh_user or "unknown"
            session_start = login.get("time", ssh_time)

            if isinstance(session_start, str):
                try:
                    session_start = datetime.fromisoformat(session_start)
                except ValueError:
                    session_start = ssh_time

            # Step 2: Find the session end (logout/disconnect), falling back to
            # the caller-supplied end_time so we don't miss trailing commands.
            session_end = end_time
            try:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT time
                        FROM {CATALOG}.silver.host_authentications
                        WHERE hostname = %(hostname)s
                          AND source_ip IN ({ip_placeholders})
                          AND action IN ('logout', 'disconnect')
                          AND time > %(session_start)s
                        ORDER BY time ASC
                        LIMIT 1
                    """, {
                        "hostname": target_hostname,
                        "session_start": str(session_start),
                    })
                    logout_rows = cur.fetchall()
                    if logout_rows and logout_rows[0][0]:
                        session_end = logout_rows[0][0]
            except Exception:
                pass  # Keep session_end = end_time

            # Step 3: Query process executions on the target host for the
            # session window and filter to the authenticated user.
            try:
                exec_rows = _query_table(
                    conn, "host_process_executions",
                    target_hostname, session_start, session_end,
                )
            except Exception:
                continue

            commands = []
            for exec_row in exec_rows:
                row_user = exec_row.get("user") or ""
                # Only include commands run by the SSH session user
                if session_user and row_user and row_user != session_user:
                    continue

                cmd_line = exec_row.get("command_line", "") or ""

                # Skip bash HISTTIMEFORMAT epoch comment lines (#1234567890)
                if cmd_line.startswith("#") and cmd_line[1:].strip().isdigit():
                    continue

                time_val = exec_row.get("time", "")
                time_str = str(time_val) if time_val is not None else ""

                _, nested_target = _parse_ssh_target(cmd_line)
                is_ssh = nested_target is not None

                commands.append({
                    "time": time_str,
                    "command_line": cmd_line,
                    "is_ssh": is_ssh,
                })

            if not commands:
                continue

            # Step 4: Store under the node ID that derive_graph() already
            # created for this SSH target IP.
            node_id = f"ssh_target:{ssh_target_ip}"
            label = f"{session_user}@{target_hostname} (SSH session)"
            if node_id not in result:
                result[node_id] = {"label": label, "commands": commands}
            else:
                # Multiple SSH commands to the same IP — merge and re-sort
                result[node_id]["commands"].extend(commands)
                result[node_id]["commands"].sort(key=lambda c: c["time"])

    except Exception:
        return {}

    return result


def build_command_index(timeline, conn=None, host_ips=None, end_time=None):
    """
    Group process_execution events by user node ID for the collapsible command
    panel UI.  Returns a dict keyed by user_node_id with the shape:
        {
          "user:ec2-user@ip-10-0-1-14": {
              "label": "ec2-user",
              "commands": [
                  {"time": "2024-01-01 12:00:00", "command_line": "ls -la", "is_ssh": False},
                  ...
              ]
          },
          ...
        }

    SSH detection: command starts with "ssh " or contains an isolated "ssh"
    followed by whitespace (e.g., "sudo ssh -i key host").
    Commands within each user are sorted ascending by time.

    When conn and host_ips are provided, also resolves SSH session commands on
    target hosts via _resolve_ssh_sessions() and merges them into the index
    under "ssh_target:{ip}" keys so clicking SSH target nodes in the vis.js
    graph opens a command panel for the remote session.
    """
    index = {}

    for row in timeline:
        if row.get("event_category") != "process_execution":
            continue

        user = row.get("user") or "unknown"
        hostname = row.get("source_host", row.get("hostname", ""))
        user_node_id = f"user:{user}@{hostname}" if hostname else f"user:{user}"

        cmd = row.get("command_line", row.get("detail", "")) or ""
        time_val = row.get("time", "")
        time_str = str(time_val) if time_val is not None else ""

        # SSH detection: literal prefix "ssh " or word-boundary "ssh" + whitespace
        is_ssh = cmd.startswith("ssh ") or bool(re.search(r"\bssh\s+-", cmd))

        if user_node_id not in index:
            index[user_node_id] = {"label": user, "commands": []}

        index[user_node_id]["commands"].append({
            "time": time_str,
            "command_line": cmd,
            "is_ssh": is_ssh,
        })

    # Sort each user's commands chronologically
    for entry in index.values():
        entry["commands"].sort(key=lambda c: c["time"])

    # Resolve SSH session commands if connection info provided.
    # Merges "ssh_target:{ip}" entries so clicking an SSH target node in the
    # vis.js graph opens a command panel showing what ran in that session.
    if conn and host_ips:
        try:
            ssh_sessions = _resolve_ssh_sessions(conn, timeline, host_ips, end_time)
            for node_id, session_data in ssh_sessions.items():
                if node_id not in index:
                    index[node_id] = session_data
                else:
                    index[node_id]["commands"].extend(session_data["commands"])
                    index[node_id]["commands"].sort(key=lambda c: c["time"])
        except Exception:
            pass  # Don't break the app if SSH correlation fails

    return index
