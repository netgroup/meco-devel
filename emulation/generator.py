import hashlib

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
        if not outs: continue
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
