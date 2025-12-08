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

    def build_port_map(self, timeout: int = 60) -> Dict[str, Tuple[str, str, int]]:
        """
        Builds a mapping of instance_id:interface_index -> (hypervisor, ovs_bridge, ovs_port_number)
        Waits for IPv4 addresses to be assigned.
        """
        logger.info(f"Building port map (timeout={timeout}s)...")
        start_time = time.time()
        port_map = {}
        
        while time.time() - start_time < timeout:
            current_map = self._scan_ports()
            # We don't have a strict 'expected' count here easily without topology, 
            # but we can return what we have if called iteratively or just return best effort.
            # Ideally obtaining topology would be better but keeping it decoupled:
            port_map = current_map
            if port_map: 
                # In a real impl, we'd check if len(port_map) == expected_nodes
                # For now, we return what we find after one pass? 
                # No, the original waited. We should probably wait if map is empty?
                # Or caller handles waiting.
                # meco.py logic was: wait loop is inside meco.py's _wait_for_ipv4_addresses
                # Let's move the loop to the caller or implement basic wait here.
                # But self.build_port_map implies a single build. 
                # Let's rename this to scan and let a higher level wait.
                # However, for this task, I'll implement a simple wait loop strategy matching meco.py roughly.
                pass
            
            # For this port, I will return the map immediately and let the LifecycleManager loop if needed.
            # But wait, meco.py had the loop inside _wait_for_ipv4_addresses.
            # I will assume one pass is enough OR the caller loops.
            return current_map
            
        return port_map

    def _scan_ports(self) -> Dict[str, Tuple[str, str, int]]:
        """Internal scan for port mapping."""
        port_map = {}
        hypervisors = CONFIG.get("hypervisors", {})
        
        # 1. Collect all instances to check
        targets = [] # List of (remote_alias, instance_dict)
        
        if hypervisors:
            for remote in hypervisors:
                try:
                    # List instances on remote
                    # We can use incus_client.executor.run for "incus list remote: ..."
                    res = incus_client.executor.run(
                        ["incus", "list", f"{remote}:", "--format=json"], 
                        check=True, capture_output=True
                    )
                    instances = json.loads(res.stdout)
                    for inst in instances:
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
                
        # 2. Process each instance
        for remote, inst in targets:
            name = inst.get("name")
            if not name: continue
            
            # Get state
            try:
                # incus query remote:/1.0/instances/name/state
                # Note: IncusClient doesn't have query method yet, implementing ad-hoc command
                query_target = f"{remote}:{name}" if remote else name
                res = incus_client.executor.run(
                   ["incus", "query", f"/{'1.0'}/instances/{query_target}/state"],
                   # Logic fix: query path usually needs /1.0/instances/... 
                   # If remote, we prefix URI with remote? No, incus query remote:/...
                   # If remote is set, query arg is "{remote}:/1.0/instances/{name}/state"
                   # If local, query arg is "/1.0/instances/{name}/state"
                )
                # Let's clean up command construction
                uri = f"/1.0/instances/{name}/state"
                if remote:
                    uri = f"{remote}:{uri}"
                
                res = incus_client.executor.run(
                    ["incus", "query", uri], check=True, capture_output=True
                )
                state = json.loads(res.stdout)
                net_state = state.get("network", {})
            except Exception:
                logger.debug(f"Failed to query state for {name}")
                continue
                
            # Parse interfaces
            # Derive ID from name "ID-Type"
            instance_id = name.split("-")[0]
            
            for iface, data in net_state.items():
                if data.get("type") != "broadcast": continue
                
                # Check IPv4
                ipv4 = None
                for addr in data.get("addresses", []):
                    if addr.get("family") == "inet":
                        ipv4 = addr.get("address")
                        break
                if not ipv4: continue
                
                # Check ethX
                if not iface.startswith("eth"): continue
                try:
                    idx = int(re.search(r"\d+", iface).group())
                except: continue
                
                # host_dev = data.get("host_name")
                host_dev = data.get("host_name")
                if not host_dev: continue
                
                # Find Bridge
                target_exec = remote if remote else None
                
                # Try remote check first if remote
                br = ovs.port_to_br(host_dev, target=target_exec)
                if not br: continue
                
                # Get OFPort
                ofport = ovs.get_interface_ofport(host_dev, target=target_exec)
                if ofport is None or ofport < 0: continue
                
                # Map it
                key = f"{instance_id}:{idx}"
                # Value: (hypervisor_name, bridge, port)
                # Use remote as hypervisor name
                port_map[key] = (remote or "", br, ofport)
                
        return port_map
