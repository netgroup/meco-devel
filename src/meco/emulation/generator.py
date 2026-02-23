def generate_cloudinit(node):
    """
    Build a #cloud-config snippet.
    """
    # Extract node details
    node_id = node.get("id", "unknown")
    node_type = node.get("type", "unknown")
    node_name = f"{node_id} ({node_type})"

    # Password handling (placeholder logic from original)
    passwd_hash = node.get(
        "passwd_hash",
        "$6$rounds=4096$mecosalt$mecoP4ssw0rdH4sh",
    )

    return f"""#cloud-config
package_update: true
packages:
  - vim
  - curl
  - wget
  - net-tools
runcmd:
  - echo "Node {node_name} started" > /var/log/meco-init.log
users:
  - name: ubuntu
passwd: {passwd_hash}
lock_passwd: false
shell: /bin/bash
ssh_authorized_keys: []
sudo: ALL=(ALL) NOPASSWD:ALL
"""


def generate_forwarding_rules(neigh_map):
    """
    Generates OpenFlow rule strings for forwarding based on adjacency.
    args:
        neigh_map: {in_port: {out_port1, out_port2, ...}}
    """
    rules = []
    for in_p, outs in neigh_map.items():
        if not outs:
            continue
        out_list = sorted(outs)
        actions = ",".join(f"output:{p}" for p in out_list)
        rules.append(f"priority=100,in_port={in_p},actions={actions}")
    return rules


def generate_arp_rules(all_ports):
    """Generates ARP broadcast rules."""
    if len(all_ports) > 1:
        arp_actions = ",".join(f"output:{p}" for p in all_ports)
        return [f"priority=150,arp,actions={arp_actions}"]
    return []


def generate_mac(node_id, port_index=0):
    """
    Generates a deterministic MAC address based on Node ID and Port Index.
    Format: 02:00:PP:00:HH:LL
    Where PP is port_index, HH:LL is node_id.
    """
    try:
        nid = int(node_id)
        # 02 (Locally Administered) : 00 : Port : 00 : High : Low
        return (
            f"02:00:{port_index & 0xFF:02x}:00:{nid >> 8 & 0xFF:02x}:{nid & 0xFF:02x}"
        )
    except (ValueError, TypeError):
        return "00:00:00:00:00:00"


def generate_ip(node_a_id, node_b_id, is_source, node_a_type="", node_b_type=""):
    """
    Generates a deterministic IP address for a link between two nodes.
    Currently optimized for Satellite-Terminal ground links.

    Subnet: 10.<terminal_node_id>.<satellite_node_id>.x
    .1 -> Satellite, .2 -> Terminal
    """
    try:
        a_id = int(node_a_id)
        b_id = int(node_b_id)

        # Identify which one is the terminal (generally the one with higher ID in testbeds,
        # or we check types if provided)
        # Using a simple heuristic for now: higher ID is likely terminal in small testbeds,
        # but better to use types if available.

        t_id, s_id = (a_id, b_id) if "terminal" in node_a_type.lower() else (b_id, a_id)

        # If types were not conclusive, fallback to a deterministic order
        if (
            "satellite" not in node_a_type.lower()
            and "terminal" not in node_a_type.lower()
        ):
            t_id, s_id = (max(a_id, b_id), min(a_id, b_id))

        ip_tail = 1 if is_source == (a_id == s_id) else 2
        return f"10.{t_id & 0xFF}.{s_id & 0xFF}.{ip_tail}"
    except (ValueError, TypeError):
        return None
