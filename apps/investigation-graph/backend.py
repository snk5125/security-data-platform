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


def derive_graph(timeline, filters=None, ip_hostname_map=None):
    """
    Convert timeline list to vis.js nodes and edges.
    filters: dict of {event_category: bool} — only include matching events.
    ip_hostname_map: dict of {ip: hostname_label} for annotating known IPs.
    Returns (nodes_list, edges_list).
    """
    filters = filters or {}
    ip_hostname_map = ip_hostname_map or {}
    nodes = {}
    edges = []
    user_event_counts = {}

    for row in timeline:
        cat = row.get("event_category", "")

        # Skip filtered-out categories
        if filters and cat in filters and not filters[cat]:
            continue

        user = row.get("user") or "unknown"
        action = row.get("action", "")
        detail = row.get("detail", "")
        source_ip = row.get("source_ip", "")
        time_str = str(row.get("time", ""))
        hostname = row.get("source_host", row.get("hostname", ""))

        # Host node
        host_id = f"host:{hostname}"
        if hostname and host_id not in nodes:
            nodes[host_id] = {
                "id": host_id, "label": hostname,
                "type": "host", "color": "#50fa7b",
                "shape": "box", "size": 25,
                "font": {"color": "#1a1a2e"},
            }

        # User node (scoped to host)
        user_node_id = f"user:{user}@{hostname}" if hostname else f"user:{user}"
        user_label = user
        if user_node_id not in nodes:
            nodes[user_node_id] = {
                "id": user_node_id, "label": user_label,
                "type": "user", "color": "#bd93f9",
                "shape": "dot", "size": 15,
                "font": {"color": "white"},
            }
            # Connect user to host
            edges.append({
                "from": host_id, "to": user_node_id,
                "color": {"color": "#50fa7b", "opacity": 0.4}, "width": 1,
            })

        user_event_counts[user_node_id] = user_event_counts.get(user_node_id, 0) + 1

        if cat == "authentication":
            # Source IP node for external IPs
            if source_ip and not source_ip.startswith(("10.", "172.16.", "192.168.", "")):
                ip_id = f"ip:{source_ip}"
                known_host = ip_hostname_map.get(source_ip, "")
                auth_ip_label = f"{known_host}\n{source_ip}" if known_host else source_ip
                if ip_id not in nodes:
                    nodes[ip_id] = {
                        "id": ip_id, "label": auth_ip_label,
                        "type": "external_ip", "color": "#ff5555",
                        "shape": "dot", "size": 20,
                        "font": {"color": "white"},
                        "title": f"{source_ip} ({known_host})" if known_host else source_ip,
                        "event_category": "authentication",
                    }
                edges.append({
                    "from": ip_id, "to": user_node_id,
                    "label": action, "color": {"color": "#ff5555"}, "width": 2,
                    "title": f"{time_str}: {action} as {user} from {source_ip}",
                    "event_category": "authentication",
                })
            elif action == "escalation":
                src = detail.split("by ")[-1].strip() if "by " in detail else user
                src_id = f"user:{src}@{hostname}" if hostname else f"user:{src}"
                edges.append({
                    "from": src_id, "to": user_node_id,
                    "label": "escalated", "color": {"color": "#ff79c6"},
                    "dashes": True, "width": 2,
                    "title": f"{time_str}: {detail}",
                    "event_category": "authentication",
                })

        elif cat == "process_execution":
            cmd_label = detail[:50] + "..." if len(detail) > 50 else detail
            proc_id = f"proc:{hash(detail + time_str)}"
            nodes[proc_id] = {
                "id": proc_id, "label": cmd_label,
                "type": "process", "color": "#f1fa8c",
                "shape": "box", "size": 12,
                "font": {"color": "#1a1a2e", "size": 11},
                "title": detail,
                "event_category": "process_execution",
            }
            edges.append({
                "from": user_node_id, "to": proc_id,
                "label": "executed", "color": {"color": "#8be9fd"},
                "title": f"{time_str}: {user} executed {detail}",
                "event_category": "process_execution",
            })

        elif cat == "account_change":
            acct_id = f"acct:{detail}"
            if acct_id not in nodes:
                nodes[acct_id] = {
                    "id": acct_id, "label": f"{action}: {detail}",
                    "type": "account_change", "color": "#ff79c6",
                    "shape": "diamond", "size": 15,
                    "font": {"color": "white"},
                    "event_category": "account_change",
                }
            edges.append({
                "from": user_node_id, "to": acct_id,
                "label": action, "color": {"color": "#f1fa8c"},
                "title": f"{time_str}: {detail}",
                "event_category": "account_change",
            })

        elif cat == "network_connection":
            is_denied = row.get("is_denied", False)

            if is_denied:
                # Denied summary — single node per port, no individual IPs
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
                        "title": detail,
                        "event_category": "network_connection",
                    }
                    edges.append({
                        "from": denied_id, "to": host_id,
                        "label": f"✕ {port_label}",
                        "color": {"color": "#ff5555", "opacity": 0.6},
                        "width": 2, "dashes": True,
                        "title": detail,
                        "event_category": "network_connection",
                    })
            else:
                # Allowed connection — show remote IP and port detail
                remote_ip = row.get("remote_ip", "")
                dst_port = row.get("dst_port", 0)
                port_label = row.get("port_label", str(dst_port))
                direction = row.get("direction", "")

                ip_id = f"netip:{remote_ip}"
                is_internal = remote_ip.startswith(("10.", "172.16.", "192.168."))
                # Annotate with known hostname if available
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
                    "from": edge_from, "to": edge_to,
                    "label": dir_label,
                    "color": {"color": "#8be9fd"},
                    "width": 1,
                    "title": f"{time_str}: {detail}",
                    "event_category": "network_connection",
                })

        elif cat == "system_event":
            sys_id = f"sys:{user}:{hash(detail + time_str)}"
            nodes[sys_id] = {
                "id": sys_id, "label": (detail[:30] if detail else "system"),
                "type": "system_event", "color": "#44475a",
                "shape": "dot", "size": 8,
                "font": {"color": "#6272a4", "size": 10},
                "event_category": "system_event",
            }
            edges.append({
                "from": user_node_id, "to": sys_id,
                "color": {"color": "#44475a"}, "width": 1,
                "title": f"{time_str}: {detail}",
                "event_category": "system_event",
            })

    # Scale user node sizes
    for key, count in user_event_counts.items():
        if key in nodes:
            nodes[key]["size"] = min(40, 15 + count)

    return list(nodes.values()), edges
