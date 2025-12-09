import logging
from config.loader import CONFIG
from infra.executors import LocalExecutor
from infra.incus import IncusClient
from network import ovs
import re
import time
import json
from collections import defaultdict
from typing import List, Dict, Tuple, Optional, Set
import subprocess
import shlex

logger = logging.getLogger("meco.network.manager")

# Initialize infrastructure clients
# In a rigorous dependency injection model, these would be passed in.
# For now, we instantiate them here or rely on globals/config.
executor = LocalExecutor()
incus_client = IncusClient(executor)

class NetworkManager:
    """
    Manages the lifecycle of the network infrastructure (bridges, profiles).
    """

    def __init__(self):
        self.defaults = CONFIG.get("defaults", {})
        self.bridge_internal = self.defaults.get("integration_bridge", "br-int")
        self.bridge_tunnel = self.defaults.get("tunnel_bridge", "br-tun")
        self.profile_container = self.defaults.get("profile_base_container", "meco-cnt")
        self.profile_vm = self.defaults.get("profile_base_vm", "meco-vm")
        self.storage_pool = self.defaults.get("storage_pool", "default")
        self.driver = self.defaults.get("bridge_driver", "openvswitch")
        self.dns_mode = self.defaults.get("dns_mode", "dynamic")

    def setup_infrastructure(self):
        """Sets up bridges and base profiles."""
        try:
            hypervisors = CONFIG.get("hypervisors", {})
            
            # --- Network Creation ---
            if not hypervisors:
                # Local only -> Just Integration Bridge
                logger.info("Setting up local network infrastructure (br-int only)...")
                if not incus_client.create_network(self.bridge_internal, driver=self.driver, dns_mode=self.dns_mode):
                     raise RuntimeError(f"Failed to create network {self.bridge_internal}")
            else:
                logger.info(f"Setting up distributed infrastructure on {len(hypervisors)} remotes...")
                needed_nets = [self.bridge_internal, self.bridge_tunnel]
                
                for remote in hypervisors:
                    for net in needed_nets:
                        full_name = f"{remote}:{net}"
                        logger.info(f"Creating {full_name}...")
                        if not incus_client.create_network(full_name, driver=self.driver, dns_mode=self.dns_mode):
                             raise RuntimeError(f"Failed to create network {full_name}")

                    # Setup profiles on this remote
                    self._setup_profiles(remote)

            if not hypervisors:
                # 2. Setup Profiles (Local)
                self._setup_profiles()
                
            logger.info("Network infrastructure setup complete.")
            return True
        except Exception as e:
            logger.error(f"Network setup failed: {e}")
            raise

    def teardown_infrastructure(self):
        """Tears down bridges and profiles."""
        hypervisors = CONFIG.get("hypervisors", {})
        
        if hypervisors:
            logger.info(f"Tearing down distributed infrastructure on {len(hypervisors)} remotes...")
            for remote in hypervisors:
                self._teardown_target(remote)
        else:
            logger.info("Tearing down local infrastructure...")
            self._teardown_target(None)
            
        logger.info("Network infrastructure torn down.")

    def _teardown_target(self, remote: str):
        """Helper to tear down resources on a specific target."""
        target_prefix = f"{remote}:" if remote else ""
        
        # 1. Delete Profiles (must be done before deleting networks they use)
        for p_name in (self.profile_container, self.profile_vm):
            incus_client.delete_profile(f"{target_prefix}{p_name}")

        # 2. Clear OVS bridges
        for br in (self.bridge_internal, self.bridge_tunnel):
            # OVS ops: use bare bridge name + target
            ovs.del_flows(br, target=remote)
            
            # Remove all ports except the bridge itself
            for p in ovs.list_ports(br, target=remote):
                if p == br: continue
                ovs.del_port(br, p, target=remote)
                # Cleanup veth pairs if needed
                try:
                    cmd = ["sudo", "ip", "link", "del", p]
                    if remote:
                         cmd = ["incus", "exec", remote, "--"] + cmd
                    executor.run(cmd, check=False)
                except: pass
            
            # Delete Incus network (use qualified name)
            incus_client.delete_network(f"{target_prefix}{br}")

    def _setup_profiles(self, remote: str = None):
        """Sets up the base profiles (container/vm) on the target remote (or local)."""
        
        # Helper to prefix remote if needed
        def qualify(name):
            return f"{remote}:{name}" if remote else name
        
        p_cnt = qualify(self.profile_container)
        p_vm = qualify(self.profile_vm)
        
        # Container Profile
        if not incus_client.profile_exists(p_cnt):
           incus_client.create_profile(p_cnt)

        # Add root disk
        incus_client.add_profile_device(
            p_cnt, "root", "disk", "path=/", f"pool={self.storage_pool}"
        )
        
        # Add eth0 to internal bridge
        incus_client.add_profile_device(
            p_cnt, "eth0", "nic", "nictype=bridged", f"parent={self.bridge_internal}"
        )

        # VM Profile (Copy of container + extras if needed)
        if not incus_client.profile_exists(p_vm):
            incus_client.copy_profile(p_cnt, p_vm)

    def apply_flows(self, bridge: str, flows: list, target: str = None):
        """Applies a list of OpenFlow rules."""
        ovs.del_flows(bridge, target=target)
        for rule in flows:
            ovs.add_flow(bridge, rule, target=target)

    def generate_flows(
        self, topo: dict, port_map: Dict[str, Tuple[str, str, int]], vlan_id: int = 3
    ) -> Dict[str, Dict[str, List[str]]]:
        """
        Generates OVS flows based on topology visibility and port mapping.
        Returns: { hypervisor: { bridge: [flow_rules...] } }
        """
        if not port_map:
            logger.warning("No port map available. Cannot generate rules.")
            return {}

        # Build node type map
        node_type = {}
        for n in topo.get("nodes", []):
            try:
                nid = int(n.get("id"))
                node_type[nid] = str(n.get("type", "")).lower()
            except: continue

        # Collect links
        links = self._collect_topology_links(topo)
        if not links:
            return {}

        def pm_entry(nid): 
            # Look for eth0 -> "nid:0"
            return port_map.get(f"{nid}:0")

        # Group terminals per satellite
        # Key: (hv, br, sat_id), Value: {sat_port, term_ports[]}
        sat_groups = {} 
        
        for a, b in links:
            a_type = node_type.get(a, "")
            b_type = node_type.get(b, "")
            
            if a_type == "satellite" and b_type == "terminal":
                sat_id, te_id = a, b
            elif b_type == "satellite" and a_type == "terminal":
                sat_id, te_id = b, a
            else:
                continue

            sat_pm = pm_entry(sat_id)
            te_pm = pm_entry(te_id)
            if not sat_pm or not te_pm: continue
            
            sat_hv, sat_br, sat_port = sat_pm
            te_hv, te_br, te_port = te_pm
            
            if sat_hv != te_hv or sat_br != te_br:
                logger.warning(f"Skipping cross-domain link {sat_id}<->{te_id}")
                continue
                
            key = (sat_hv, sat_br, sat_id)
            if key not in sat_groups:
                sat_groups[key] = {"sat_port": sat_port, "term_ports": []}
            sat_groups[key]["term_ports"].append(te_port)

        # Assemble rules
        hv_bridge_rules = defaultdict(lambda: defaultdict(list))
        baseline_cache = set()
        
        hv_br_ports = defaultdict(set)
        for (hv, br, _), grp in sat_groups.items():
            hv_br_ports[(hv, br)].add(grp["sat_port"])
            hv_br_ports[(hv, br)].update(grp["term_ports"])

        for (hv, br, sat_id), grp in sat_groups.items():
            sat_port = grp["sat_port"]
            term_ports = sorted(set(grp["term_ports"]))
            if not term_ports: continue
            
            if (hv, br) not in baseline_cache:
                # ARP Rules
                all_ports = sorted(hv_br_ports[(hv, br)])
                for in_p in all_ports:
                    other_ports = [p for p in all_ports if p != in_p]
                    if other_ports:
                        out_str = ",".join(f"output:{p}" for p in other_ports)
                        hv_bridge_rules[hv][br].append(
                            f"table=0,priority=200,in_port={in_p},arp,actions={out_str}"
                        )
                
                # Baseline Tables
                hv_bridge_rules[hv][br].extend([
                    "table=22,priority=0,actions=drop",
                    "table=20,priority=0,actions=resubmit(,5)",
                    "table=5,priority=0,actions=resubmit(,22)",
                    (
                        "table=9,priority=1,actions="
                        "learn(table=20,priority=1,hard_timeout=60,"
                        "NXM_OF_VLAN_TCI[0..11],"
                        "NXM_OF_ETH_DST[]=NXM_OF_ETH_SRC[],"
                        "load:NXM_OF_VLAN_TCI[]->NXM_OF_VLAN_TCI[],"
                        "output:NXM_OF_IN_PORT[])"
                    )
                ])
                baseline_cache.add((hv, br))
                
            # SAT -> Tag -> Table 20
            hv_bridge_rules[hv][br].append(
                f"cookie=0x102,table=0,priority=100,in_port={sat_port},vlan_tci=0,"
                f"actions=load:0x{vlan_id:x}->NXM_OF_VLAN_TCI[],resubmit(,20)"
            )
            
            # Downlink Table 5 -> Strip Tag -> Terminals
            actions = []
            for te_p in term_ports:
                actions.append("load:0->NXM_OF_VLAN_TCI[]")
                actions.append(f"output:{te_p}")
            hv_bridge_rules[hv][br].append(
                f"cookie=0x103,table=5,priority=100,vlan_tci=0x{vlan_id:x}/0x0fff,actions=" + ",".join(actions)
            )
            
            # Uplink Term -> Tag -> Learn -> Strip -> SAT
            for idx, te_p in enumerate(term_ports):
                cookie = 0x100 + idx
                hv_bridge_rules[hv][br].append(
                    f"cookie=0x{cookie:x},table=0,priority=100,in_port={te_p},vlan_tci=0,"
                    f"actions=load:0x{vlan_id:x}->NXM_OF_VLAN_TCI[],resubmit(,9),load:0->NXM_OF_VLAN_TCI[],output:{sat_port}"
                )
                
        # Convert defaultdict to dict for cleaner return 
        # (Though defaultdict is fine, the signature says Dict)
        return {h: dict(b) for h, b in hv_bridge_rules.items()}


    def generate_of13_rules_from_visibility(
        self, topo: dict, vlan_id: int | None = None
    ) -> Dict[str, Dict[str, List[str]]]:
        """
        Build OpenFlow 1.3 rules per hypervisor/bridge from the topology visibility.

        Returns a mapping: { hypervisor: { bridge: [flow_string, ...] } }
        """
        # Compute current port mappings (includes hypervisor, bridge, ofport)
        # Using build_port_map as the waiter
        expected_nodes = len(topo.get("nodes", []))
        port_map = self.build_port_map(expected_count=expected_nodes, timeout=120)
        if not port_map:
            logger.error("[OF13] No port map available. Cannot generate rules.")
            return {}

        # Build node type map by id
        node_type: Dict[int, str] = {}
        for n in topo.get("nodes", []):
            try:
                nid = int(n.get("id"))
                node_type[nid] = str(n.get("type", "")).lower()
            except Exception:
                continue

        # Collect visibility links (undirected for grouping; direction used when building rules)
        links = self._collect_topology_links(topo)
        if not links:
            logger.warning("[OF13] No visibility links; nothing to generate.")
            return {}

        # Index port_map entries by node id for eth0 ("id:0")
        def pm_entry(node_id: int):
            return port_map.get(f"{node_id}:0")  # (hv, br, port)

        # Group terminals per satellite on each (hypervisor,bridge)
        # sat_groups[(hv,br,sat_id)] = { 'sat_port': int, 'term_ports': [int,...] }
        sat_groups: dict[tuple[str, str, int], dict[str, object]] = {}

        for a, b in links:
            a_type = node_type.get(a, "")
            b_type = node_type.get(b, "")
            # Normalise as satellite-terminal pairs; ignore other types for now
            if a_type == "satellite" and b_type == "terminal":
                sat_id, te_id = a, b
            elif b_type == "satellite" and a_type == "terminal":
                sat_id, te_id = b, a
            else:
                # Skip unsupported link types in this helper
                continue

            sat_pm = pm_entry(sat_id)
            te_pm = pm_entry(te_id)
            if not sat_pm or not te_pm:
                logger.debug(f"[OF13] Skip pair {sat_id}<->{te_id}: missing port map.")
                continue
            sat_hv, sat_br, sat_port = sat_pm
            te_hv, te_br, te_port = te_pm

            # Must be on same hypervisor and bridge to wire directly
            if sat_hv != te_hv or sat_br != te_br:
                logger.warning(
                    f"[OF13] {sat_id}(sat) and {te_id}(term) on different domains: {sat_hv}/{sat_br} vs {te_hv}/{te_br}; skipping."
                )
                continue

            key = (sat_hv, sat_br, sat_id)
            grp = sat_groups.get(key)
            if not grp:
                grp = {"sat_port": sat_port, "term_ports": []}
                sat_groups[key] = grp
            grp["term_ports"].append(te_port)

        # Assemble flows per hypervisor/bridge
        hv_bridge_rules: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )

        # Default VLAN id (matches example if not provided)
        vlan = vlan_id if vlan_id is not None else 3

        # Prepare shared baseline + learn rule per hv/bridge (add once)
        baseline_cache = set()  # (hv, br)
        
        # Collect all ports per (hv, br) for ARP handling
        hv_br_ports: dict[tuple[str, str], set[int]] = defaultdict(set)
        for (hv, br, sat_id), grp in sat_groups.items():
            hv_br_ports[(hv, br)].add(grp["sat_port"])
            hv_br_ports[(hv, br)].update(grp["term_ports"])

        for (hv, br, sat_id), grp in sat_groups.items():
            sat_port = grp["sat_port"]
            term_ports: list[int] = sorted(set(grp["term_ports"]))
            if not term_ports:
                continue

            # 0) Baseline pipeline tables, ARP rules, and learn rule (once per hv/br)
            if (hv, br) not in baseline_cache:
                # Add ARP broadcast rules FIRST (highest priority at table 0)
                all_ports_here = sorted(hv_br_ports[(hv, br)])
                logger.info(f"[OF13] Adding ARP rules for {hv}/{br} with ports: {all_ports_here}")
                
                for in_p in all_ports_here:
                    other_ports = [p for p in all_ports_here if p != in_p]
                    if other_ports:
                        arp_output_str = ",".join(f"output:{p}" for p in other_ports)
                        hv_bridge_rules[hv][br].append(
                            f"table=0,priority=200,in_port={in_p},arp,actions={arp_output_str}"
                        )
                
                # Then add baseline tables
                hv_bridge_rules[hv][br].extend(
                    [
                        # Default drop table
                        "table=22,priority=0,actions=drop",
                        # Unicast resolution table (learned entries). If miss, go to flood/mirror table 5.
                        "table=20,priority=0,actions=resubmit(,5)",
                        # Flood/mirror table. If miss here, drop.
                        "table=5,priority=0,actions=resubmit(,22)",
                        # Learn rule: TERM source MAC gets learned to table 20
                        (
                            "table=9,priority=1,actions="
                            "learn(table=20,priority=1,hard_timeout=60,"
                            "NXM_OF_VLAN_TCI[0..11],"
                            "NXM_OF_ETH_DST[]=NXM_OF_ETH_SRC[],"
                            "load:NXM_OF_VLAN_TCI[]->NXM_OF_VLAN_TCI[],"
                            "output:NXM_OF_IN_PORT[])"
                        ),
                    ]
                )
                baseline_cache.add((hv, br))

            # 1) SAT -> pipeline entry (table 0) with VLAN tag, then consult learned unicast (table 20)
            hv_bridge_rules[hv][br].append(
                f"cookie=0x102,table=0,priority=100,in_port={sat_port},vlan_tci=0,"
                f"actions=load:0x{vlan:x}->NXM_OF_VLAN_TCI[],resubmit(,20)"
            )

            # 2) Downlink replicate SAT -> all terminals for this group (clear VLAN before output)
            actions = []
            for te_p in term_ports:
                actions.append("load:0->NXM_OF_VLAN_TCI[]")
                actions.append(f"output:{te_p}")
            hv_bridge_rules[hv][br].append(
                "cookie=0x103,table=5,priority=100,"
                f"vlan_tci=0x{vlan:x}/0x0fff,actions=" + ",".join(actions)
            )

            # 3-4) Uplink TERM -> SAT per terminal: tag, learn (resubmit to 9), clear tag, output SAT
            for idx, te_p in enumerate(term_ports, start=0):
                cookie = 0x100 + idx  # simple per-entry cookie
                hv_bridge_rules[hv][br].append(
                    f"cookie=0x{cookie:x},table=0,priority=100,in_port={te_p},vlan_tci=0,"
                    f"actions=load:0x{vlan:x}->NXM_OF_VLAN_TCI[],resubmit(,9),load:0->NXM_OF_VLAN_TCI[],output:{sat_port}"
                )

        return {hv: dict(br_map) for hv, br_map in hv_bridge_rules.items()}


    def _collect_topology_links(self, topo: dict) -> List[Tuple[int, int]]:
        links = []
        for section in ("visibility-constellation", "visibility-ground"):
            for snap in topo.get(section, []):
                for conn in snap.get("connection", []):
                    src = conn.get("source")
                    dst = conn.get("destination")
                    if src is not None and dst is not None:
                        links.append((src, dst))
        return links


    def build_port_map(self, expected_count: int = 0, timeout: int = 120) -> Dict[str, Tuple[str, str, int]]:
        """
        Builds a mapping of instance_id:interface_index -> (hypervisor, ovs_bridge, ovs_port_number)
        Waits for IPv4 addresses to be assigned.
        """
        logger.info(f"Building port map (expecting ~{expected_count} nodes, timeout={timeout}s)...")
        start_time = time.time()
        port_map = {}
        
        while time.time() - start_time < timeout:
            current_map = self._scan_ports()
            port_map = current_map
            
            got = len(port_map)
            # If we expect N nodes, we expect at least N eth0 interfaces approx.
            # Ideally each node has >=1 interface.
            if expected_count > 0 and got >= expected_count:
                 logger.info(f"Port map complete: found {got}/{expected_count} interfaces.")
                 return port_map
                 
            # Log progress periodically?
            if int(time.time() - start_time) % 5 == 0:
                logger.info(f"Waiting for ports... found {got}/{expected_count}")
            
            time.sleep(2)
            
        logger.warning(f"Timeout waiting for ports. Found {len(port_map)}/{expected_count}.")
        return port_map

    def _scan_ports(self) -> Dict[str, Tuple[str, str, int]]:
        """Internal scan for port mapping."""
        port_map = {}
        hypervisors = CONFIG.get("hypervisors", {})
        logger.debug(f"Scanning ports. Configured hypervisors: {list(hypervisors.keys())}")
        
        # 1. Collect all instances to check
        targets = [] # List of (remote_alias, instance_dict)
        
        if hypervisors:
            for remote in hypervisors:
                try:
                    res = incus_client.executor.run(
                        ["incus", "list", f"{remote}:", "--format=json"], 
                        check=True, capture_output=True
                    )
                    instances = json.loads(res.stdout)
                    logger.debug(f"Found {len(instances)} instances on {remote}")
                    for inst in instances:
                        # User snippet suggests including all instances and filtering later or relying on 'user.meco' being present.
                        # However, meco-27oct.py snippet logic: adds ALL to remote_instances list.
                        # But then iterates them. 
                        # To be safe and trusting the user's advice: include ALL running instances from remote
                        # OR check for meco config existence loosely.
                        # Let's try to include if status is Running, regardless of config for now, 
                        # or log why it's skipped.
                        
                        # Debug log config
                        # logger.debug(f"Instance {inst.get('name')} config: {inst.get('config', {}).get('user_meco')}")
                        
                        if inst.get("status") == "Running":
                             targets.append((remote, inst))

                except Exception as e:
                    logger.warning(f"Failed to list instances on {remote}: {e}")
        else:
            # Local
            try:
                instances = incus_client.list_instances()
                # Filter for meco instances
                meco_instances = [
                    i for i in instances 
                    if i.get("config", {}).get("user.meco") == "true" 
                    and i.get("status") == "Running"
                ]
                for inst in meco_instances:
                    targets.append((None, inst))
            except Exception:
                pass
        
        if not targets:
            logger.debug("No running MECO instances found during scan.")
            return {}

        # 2. Process each instance
        for remote, inst in targets:
            name = inst.get("name")
            if not name: continue
            
            # Get state
            try:
                uri = f"/1.0/instances/{name}/state"
                if remote:
                    uri = f"{remote}:{uri}"
                
                res = incus_client.executor.run(
                    ["incus", "query", uri], check=True, capture_output=True
                )
                state = json.loads(res.stdout)
                net_state = state.get("network", {})
            except Exception as e:
                logger.debug(f"Failed to query state for {name} on {remote}: {e}")
                continue
                
            # Parse interfaces
            # Derive ID from name "ID-Type"
            try:
               instance_id = name.split("-")[0]
               int(instance_id) # Validate it is int
            except ValueError:
               continue
            
            for iface, data in net_state.items():
                if data.get("type") != "broadcast": continue
                
                # Check IPv4
                ipv4 = None
                for addr in data.get("addresses", []):
                    if addr.get("family") == "inet":
                        ipv4 = addr.get("address")
                        break
                if not ipv4: 
                    logger.debug(f"No IPv4 for {name} {iface}")
                    continue
                
                # Check ethX
                if not iface.startswith("eth"): continue
                
                try:

                    # Parse index
                    idx = int(re.findall(r"\d+", iface)[0])
                    
                    # Get host device
                    host_dev = data.get("host_name")
                    if not host_dev: continue

                    # Resolve bridge and OFPort
                    br = None
                    ofport = None

                    if remote:
                         # Remote: Use SSH to resolve
                         br, ofport = self._resolve_remote_port(remote, host_dev)
                    else:
                         # Local: Use OVS helper
                         br = ovs.port_to_br(host_dev)
                         ofport = ovs.get_interface_ofport(host_dev)

                    if not br: continue
                    if ofport is None or ofport < 0: continue
                    
                    # Key: "id:index" -> (hv, br, ofport)
                    # If remote is None, we need to know local hv name? 
                    # Actually generate_of13 rules handles 'hv' as a key.
                    # If remote is None, we should use a default name or 'local'?
                    # In meco.py it used 'local' if no hypervisors.
                    hv_key = remote if remote else "local"
                    
                    port_map[f"{instance_id}:{idx}"] = (hv_key, br, ofport)
                    logger.debug(f"Mapped {name}/{iface} -> {hv_key}/{br}/{ofport}")

                except Exception:
                    continue
                    
        return port_map

    def _resolve_remote_port(self, hypervisor: str, interface: str) -> Tuple[Optional[str], Optional[int]]:
        """
        Resolves the OVS bridge and OFPort for a given interface on a remote hypervisor.
        Uses direct SSH commands to avoid 'incus exec' nesting issues.
        """
        ssh_addr = self._get_hypervisor_ssh_addr(hypervisor)
        if not ssh_addr: 
            return None, None

        # Helper to run SSH command
        def run_ssh(cmd_list):
            try:
                # Use batch mode, short timeout
                ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", ssh_addr] + cmd_list
                res = executor.run(ssh_cmd, check=False, capture_output=True)
                if res.returncode == 0:
                     return res.stdout.strip()
            except Exception as e:
                logger.debug(f"SSH to {ssh_addr} failed: {e}")
            return None

        # 1. Get Bridge
        # sudo ovs-vsctl port-to-br <interface>
        br_name = run_ssh(["sudo", "ovs-vsctl", "port-to-br", interface])
        if not br_name:
            return None, None
            
        # 2. Get OFPort
        # sudo ovs-vsctl get Interface <interface> ofport
        ofport_str = run_ssh(["sudo", "ovs-vsctl", "get", "Interface", interface, "ofport"])
        
        try:
            ofport = int(ofport_str)
            return br_name, ofport
        except (ValueError, TypeError):
            return br_name, None

    def _get_hypervisor_ssh_addr(self, hypervisor: str) -> str:
        """Returns the SSH host/IP for a hypervisor."""
        hv_config = CONFIG.get("hypervisors", {}).get(hypervisor, {})
        return hv_config.get("host") or hv_config.get("ip") or hypervisor

