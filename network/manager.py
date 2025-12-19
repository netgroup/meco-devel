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
                if not incus_client.create_network(
                    self.bridge_internal, driver=self.driver, dns_mode=self.dns_mode
                ):
                    raise RuntimeError(
                        f"Failed to create network {self.bridge_internal}"
                    )
            else:
                logger.info(
                    f"Setting up distributed infrastructure on {len(hypervisors)} remotes..."
                )
                needed_nets = [self.bridge_internal, self.bridge_tunnel]

                for remote in hypervisors:
                    for net in needed_nets:
                        full_name = f"{remote}:{net}"
                        logger.info(f"Creating {full_name}...")
                        if not incus_client.create_network(
                            full_name, driver=self.driver, dns_mode=self.dns_mode
                        ):
                            raise RuntimeError(f"Failed to create network {full_name}")

                    # Setup profiles on this remote
                    self._setup_profiles(remote)
                    self._setup_patch_ports(remote)

                    # Setup Tunnels (Full Mesh)
                    self._setup_tunnels(remote, hypervisors)

                    # Apply Base Rules
                    for net in needed_nets:
                        base_flows = self.generate_base_rules(net)
                        self.apply_flows(
                            net, base_flows, target=remote, clear_existing=True
                        )

            if not hypervisors:
                # 2. Setup Profiles (Local)
                self._setup_profiles()
                self._setup_patch_ports()

                # Apply Base Rules (Local)
                needed_nets = [self.bridge_internal, self.bridge_tunnel]
                for net in needed_nets:
                    base_flows = self.generate_base_rules(net)
                    self.apply_flows(net, base_flows, clear_existing=True)

            logger.info("Network infrastructure setup complete.")
            return True
        except Exception as e:
            logger.error(f"Network setup failed: {e}")
            raise

    def teardown_infrastructure(self):
        """Tears down bridges and profiles."""
        hypervisors = CONFIG.get("hypervisors", {})

        if hypervisors:
            logger.info(
                f"Tearing down distributed infrastructure on {len(hypervisors)} remotes..."
            )
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
                if p == br:
                    continue
                ovs.del_port(br, p, target=remote)
                # Cleanup veth pairs if needed
                try:
                    cmd = ["sudo", "ip", "link", "del", p]
                    if remote:
                        cmd = ["incus", "exec", remote, "--"] + cmd
                    executor.run(cmd, check=False)
                except:
                    pass

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

    def _setup_patch_ports(self, remote: str = None):
        """Creates patch ports between br-int and br-tun."""
        # br-int: patch-tun -> br-tun
        # br-tun: patch-int -> br-int
        logger.info(f"Setting up patch ports on {remote or 'local'}...")

        ok1 = ovs.add_patch_port(
            self.bridge_internal, "patch-tun", "patch-int", target=remote
        )
        ok2 = ovs.add_patch_port(
            self.bridge_tunnel, "patch-int", "patch-tun", target=remote
        )

        if not (ok1 and ok2):
            logger.warning(f"Failed to setup patch ports nicely on {remote}")

    def _setup_tunnels(self, current_hv: str, all_hypervisors: dict):
        """
        Sets up VXLAN tunnels from current_hv to all other hypervisors.
        Port name format: vxlan-<remote_alias>
        """
        if not current_hv:
            return  # Local only setup handled differently or ignored?

        logger.info(f"Setting up tunnels on {current_hv}...")

        for other_hv, data in all_hypervisors.items():
            if other_hv == current_hv:
                continue

            # Get peer IP
            peer_ip = data.get("ip") or data.get("host")
            if not peer_ip:
                logger.warning(f"Skipping tunnel to {other_hv} (no IP found)")
                continue

            port_name = f"vxlan-{other_hv}"
            logger.info(f"Creating tunnel {port_name} on {current_hv} -> {peer_ip}")

            # Add port to br-tun
            # Use key=flow (default in ovs helper is flow) or fixed key?
            # User design doc says: "TUN_ID: VXLAN Tunnel ID (VNI) for remote connections (e.g., 0x17)"
            # This implies flow-based tunneling (options:key=flow) where we set tunnel_id in flow actions.
            ovs.add_vxlan_port(
                self.bridge_tunnel, port_name, peer_ip, key="flow", target=current_hv
            )

    def generate_base_rules(self, bridge: str) -> List[str]:
        """
        Returns static initialized rules for br-int or br-tun.
        """
        rules = []
        if bridge == self.bridge_internal:
            # --- br-int ---
            # [Table 0] DHCP Bypass (Allow 67/68 to NORMAL)
            # Essential for initial IP assignment before custom rules kick in.
            rules.append("table=0,priority=1000,udp,tp_src=68,tp_dst=67,actions=NORMAL")
            rules.append("table=0,priority=1000,udp,tp_src=67,tp_dst=68,actions=NORMAL")

            # [Table 0] Ingress from Patch Port (Remote Traffic)
            rules.append("table=0,priority=1,in_port=patch-tun,actions=resubmit(,4)")

            # [Table 4] Remote Traffic Processing
            # Default: Resubmit to Table 20 (Unicast Auto-learning)
            rules.append("table=4,priority=0,actions=resubmit(,20)")
            # Handle Broadcast/Multicast (BUM) from Remote
            rules.append(
                "table=4,priority=1,dl_dst=01:00:00:00:00:00/01:00:00:00:00:00,actions=resubmit(,22)"
            )

            # [Table 5] Local Traffic Processing
            # Default: Resubmit to Table 20
            rules.append("table=5,priority=0,actions=resubmit(,20)")
            # Handle Broadcast/Multicast (BUM) locally
            rules.append(
                "table=5,priority=1,dl_dst=01:00:00:00:00:00/01:00:00:00:00:00,actions=resubmit(,22)"
            )

            # [Table 9] Learning Logic (Term -> Sat)
            # Learn Source MAC and VLAN
            rules.append(
                "table=9,priority=1,actions="
                "learn(table=20,priority=1,hard_timeout=60,"
                "NXM_OF_VLAN_TCI[0..11],"
                "NXM_OF_ETH_DST[]=NXM_OF_ETH_SRC[],"
                "load:NXM_OF_VLAN_TCI[]->NXM_OF_VLAN_TCI[],"
                "output:NXM_OF_IN_PORT[])"
            )

            # [Table 20] Unicast Auto-Learning Fallback -> BUM
            rules.append("table=20,priority=0,actions=resubmit(,22)")

            # [Table 22] Flooding/Drop -> Default Drop
            rules.append("table=22,priority=0,actions=drop")

        elif bridge == self.bridge_tunnel:
            # --- br-tun ---
            # [Table 0] Ingress from Patch Port (Local Traffic)
            rules.append("table=0,priority=1,in_port=patch-int,actions=resubmit(,5)")

            # [Table 5] Mirror br-int structure
            rules.append("table=5,priority=0,actions=resubmit(,20)")
            rules.append(
                "table=5,priority=1,dl_dst=01:00:00:00:00:00/01:00:00:00:00:00,actions=resubmit(,22)"
            )

            # [Table 20]
            rules.append("table=20,priority=0,actions=resubmit(,22)")

            # [Table 22] Drop
            rules.append("table=22,priority=0,actions=drop")

            # [Table 9] Learning Logic (Remote -> Local) - Map VXLAN ID
            rules.append(
                "table=9,priority=1,actions="
                "learn(table=20,priority=1,hard_timeout=60,"
                "NXM_OF_VLAN_TCI[0..11],"
                "NXM_OF_ETH_DST[]=NXM_OF_ETH_SRC[],"
                "load:0->NXM_OF_VLAN_TCI[],"
                "load:NXM_NX_TUN_ID[]->NXM_NX_TUN_ID[],"
                "output:NXM_OF_IN_PORT[])"
            )

        return rules

    def apply_flows(
        self, bridge: str, flows: list, target: str = None, clear_existing: bool = False
    ):
        """
        Applies a list of OpenFlow rules.
        :param clear_existing: If True, deletes all flows on the bridge before applying.
        """
        if clear_existing:
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
            except:
                continue

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
            if not sat_pm or not te_pm:
                continue

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
            if not term_ports:
                continue

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
                hv_bridge_rules[hv][br].extend(
                    [
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
                        ),
                    ]
                )
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
                f"cookie=0x103,table=5,priority=100,vlan_tci=0x{vlan_id:x}/0x0fff,actions="
                + ",".join(actions)
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

    def generate_visibility_rules(
        self, topo: dict, vlan_id: int = 0x03, tun_id: int = 0x17
    ) -> Dict[str, Dict[str, List[str]]]:
        """
        Generates OpenFlow rules based on visibility (Scenarios A, B, C).
        Refactored to follow "Rules Template Structure" with specific cookie/VLAN management.
        """
        port_map = self.build_port_map(
            expected_count=len(topo.get("nodes", [])), timeout=120
        )
        if not port_map:
            logger.error("No port map available. Cannot generate visibility rules.")
            return {}

        node_type: Dict[int, str] = {}
        for n in topo.get("nodes", []):
            try:
                node_type[int(n.get("id"))] = str(n.get("type", "")).lower()
            except:
                continue

        links = self._collect_topology_links(topo)

        # Helper to get port info: (hv, br, port)
        def pm(nid):
            return port_map.get(f"{nid}:0")

        # Result structure: hv -> bridge -> rules
        rules: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))

        # 1. Group nodes by Hypervisor and Satellite
        # We need a structure: hv -> satellites -> [local_terms, remote_terms_by_hv]
        # This aligns with the "Group nodes by hypervisor" logic in the user's snippet.

        # Determine Satellites and their connections
        sat_data = defaultdict(lambda: {"local_terms": [], "remote_terms": []})
        # Key: (sat_hv, sat_id), Value: lists of term dictionaries

        # Assign unique VLAN/Tunnel ID per Satellite
        # Start at 0x10 or so to avoid conflicts with reserved VLANs
        current_vlan = 0x10
        sat_conf = {}  # sat_id -> {vlan, tun, cookie_base}

        for a, b in links:
            a_type, b_type = node_type.get(a), node_type.get(b)
            if a_type == "satellite" and b_type == "terminal":
                sat_id, term_id = a, b
            elif b_type == "satellite" and a_type == "terminal":
                sat_id, term_id = b, a
            else:
                continue

            # Resolve Satellite
            s_info = pm(sat_id)
            if not s_info:
                continue
            sat_hv, sat_br, sat_port = s_info

            # Resolve Terminal
            t_info = pm(term_id)
            if not t_info:
                continue
            term_hv, term_br, term_port = t_info

            # Init config if missing
            if sat_id not in sat_conf:
                sat_conf[sat_id] = {
                    "vlan": current_vlan,
                    "tun": current_vlan + 1000,  # arbitrary mapping
                    "cookie_base": sat_id,  # Use ID as base (e.g. 1 -> 0x1..)
                }
                current_vlan += 1

            term_obj = {
                "id": term_id,
                "hv": term_hv,
                "br": term_br,
                "port": term_port,
                "sat_id": sat_id,  # Link back
            }

            if sat_hv == term_hv:
                sat_data[(sat_hv, sat_id)]["local_terms"].append(term_obj)
            else:
                sat_data[(sat_hv, sat_id)]["remote_terms"].append(term_obj)

        # 2. Generate Rules using Template Logic
        for (sat_hv, sat_id), conn_data in sat_data.items():
            conf = sat_conf[sat_id]
            s_vlan = conf["vlan"]
            s_tun = conf["tun"]
            c_base = conf["cookie_base"]  # ID (int)

            # Resolve Sat Port again
            _, _, sat_port = pm(sat_id)

            local_terms = conn_data["local_terms"]
            remote_terms = conn_data["remote_terms"]

            # --- LOCAL SATELLITE RULES (br-int) ---

            # 1. Local Downlink (Sat -> VLAN -> Resubmit 5)
            # Template: 0x{{cookie_base}}00
            c_down = f"0x{c_base}00"
            rules[sat_hv]["br-int"].append(
                f"cookie={c_down},table=0,priority=1,in_port={sat_port},vlan_tci=0,"
                f"actions=load:0x{s_vlan:x}->NXM_OF_VLAN_TCI[],resubmit(,5)"
            )

            # 2. Terminal Uplinks (Local Terms only per template)
            # Template: 0x{{cookie_base}}01
            # "for each local terminal: generate terminal_uplink"
            c_up = f"0x{c_base}01"
            for t in local_terms:
                rules[sat_hv]["br-int"].append(
                    f"cookie={c_up},table=0,priority=1,in_port={t['port']},vlan_tci=0,"
                    f"actions=load:0x{s_vlan:x}->NXM_OF_VLAN_TCI[],resubmit(,9),"
                    f"load:0->NXM_OF_VLAN_TCI[],output:{sat_port}"
                )

            # 3. Remote Downlink Distribution (Table 22)
            # "for each remote terminal... generate remote_downlink_to_bridge"
            # Actually template groups them all in one rule?
            # "actions: [local terms], [patch-tun if remote]"
            # This rule triggers on Sat Port + VLAN match in Table 22.
            # So one rule per Satellite.

            actions_22 = []
            # Local terminals
            for t in local_terms:
                actions_22.append("load:0->NXM_OF_VLAN_TCI[]")
                actions_22.append(f"output:{t['port']}")

            # Remote terminals (output to tunnel bridge)
            # Logic: If ANY remote terminals exist, flood to patch-tun?
            # Or strict? Template says "Remote terminals (via tunnel)... load:vlan... output:patch-tun"
            if remote_terms:
                actions_22.append(f"load:0x{s_vlan:x}->NXM_OF_VLAN_TCI[]")
                actions_22.append("output:patch-tun")

            if actions_22:
                c_dist = f"0x{c_base}02"
                rules[sat_hv]["br-int"].append(
                    f"cookie={c_dist},table=22,priority=1,in_port={sat_port},vlan_tci=0x{s_vlan:x}/0x0fff,"
                    f"actions={','.join(actions_22)}"
                )

            # 7. Remote Terminals -> Satellite (Reception on Sat HV)
            # Packet arrives from patch-tun with s_vlan.
            # Base rules send it to Table 22.
            # We need to catch it and deliver to sat_port.
            if remote_terms:
                c_recv_remote = f"0x{c_base}08"
                rules[sat_hv]["br-int"].append(
                    f"cookie={c_recv_remote},table=22,priority=1,in_port=patch-tun,vlan_tci=0x{s_vlan:x}/0x0fff,"
                    f"actions=load:0->NXM_OF_VLAN_TCI[],output:{sat_port}"
                )

            # --- REMOTE CROSS-HYPERVISOR RULES (br-tun on Sat Node) ---

            # 4. Br-Tun VXLAN Egress (Table 22)
            # "for each remote terminal... generate br_tun_vxlan_egress"
            # Template: Match VLAN. Action: Load TunID, Output VXLAN-Port.
            # If multiple remote HVs, each needs a rule?
            # But VLAN is the match. If we output to multiple HVs, we need multiple actions or flow splitting.
            # If "match: vlan_tci", then one rule must handle ALL destinations or we need specific masks?
            # Flow-based tunneling: we can output to multiple ports.
            # Or does the user template imply one rule per remote HV?
            # "for each remote terminal... generate".
            # If we have terminals on HV2 and HV3.
            # Rule match: VLAN. Action: Output vxlan-hv2, output vxlan-hv3?
            # Wait, `vxlan_tunnel_id` loading must happen before output.
            # `load:tun->ID, output:p1, load:tun->ID, output:p2` ? Yes.

            # Collect remote HVs
            remote_hvs = set(t["hv"] for t in remote_terms)

            egress_actions = []
            for rhv in remote_hvs:
                vxlan_port = f"vxlan-{rhv}"
                egress_actions.append(f"load:0->NXM_OF_VLAN_TCI[]")
                egress_actions.append(f"load:0x{s_tun:x}->NXM_NX_TUN_ID[]")
                egress_actions.append(f"output:{vxlan_port}")

            if egress_actions:
                c_egress = f"0x{c_base}04"
                rules[sat_hv]["br-tun"].append(
                    f"cookie={c_egress},table=22,priority=1,vlan_tci=0x{s_vlan:x}/0x0fff,"
                    f"actions={','.join(egress_actions)}"
                )

            # --- REMOTE SIDE RULES (On Terminal Hypervisors) ---

            # Add Ingress Rule on Satellite Hypervisor (Return Path)
            # If we have remote terminals, Sat HV needs to accept their TunID packets.
            if remote_hvs:
                c_ingress_sat = f"0x{c_base}06"
                rules[sat_hv]["br-tun"].append(
                    f"cookie={c_ingress_sat},table=0,priority=1,tun_id=0x{s_tun:x},"
                    f"actions=load:0x{s_vlan:x}->NXM_OF_VLAN_TCI[],resubmit(,9),output:patch-int"
                )

            for rhv in remote_hvs:
                # Find terminals on this RHV for this Sat
                my_terms = [t for t in remote_terms if t["hv"] == rhv]
                rhv_actions = []
                for t in my_terms:
                    rhv_actions.append("load:0->NXM_OF_VLAN_TCI[]")
                    rhv_actions.append(f"output:{t['port']}")

                # 5. Remote Br-Tun Ingress (Tunnel -> Patch) (Forward Path)
                # RHV needs to accept Sat TunID packets.
                c_ingress = f"0x{c_base}05"
                rules[rhv]["br-tun"].append(
                    f"cookie={c_ingress},table=0,priority=1,tun_id=0x{s_tun:x},"
                    f"actions=load:0x{s_vlan:x}->NXM_OF_VLAN_TCI[],output:patch-int"
                )

                # Remote Br-Tun Egress (Patch -> Tunnel) (Return Path)
                # RHV needs to send Sat VLAN packets back to Sat HV.
                # Match VLAN -> Set TunID -> Output VXLAN-SatHV
                vxlan_port_sat = f"vxlan-{sat_hv}"
                c_egress_rhv = f"0x{c_base}07"
                rules[rhv]["br-tun"].append(
                    f"cookie={c_egress_rhv},table=22,priority=1,vlan_tci=0x{s_vlan:x}/0x0fff,"
                    f"actions=load:0->NXM_OF_VLAN_TCI[],load:0x{s_tun:x}->NXM_NX_TUN_ID[],output:{vxlan_port_sat}"
                )

                if rhv_actions:
                    # Let's use c_dist (0x..02) style but for remote reception
                    rules[rhv]["br-int"].append(
                        f"cookie={c_dist},table=22,priority=1,vlan_tci=0x{s_vlan:x}/0x0fff,"
                        f"actions={','.join(rhv_actions)}"
                    )

                # 6. Remote Uplink (Term -> Sat)
                # Term -> Br-Int Table 0 -> Load VLAN -> Output patch-tun
                # Matches `terminal_uplink` logic but output is patch-tun.
                # FIX: Do NOT strip VLAN (load:0) before outputting to patch-tun,
                # so br-tun can see the VLAN.
                for t in my_terms:
                    rules[rhv]["br-int"].append(
                        f"cookie={c_up},table=0,priority=1,in_port={t['port']},vlan_tci=0,"
                        f"actions=load:0x{s_vlan:x}->NXM_OF_VLAN_TCI[],resubmit(,9),"
                        f"output:patch-tun"
                    )

        return dict(rules)

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

    def build_port_map(
        self, expected_count: int = 0, timeout: int = 120
    ) -> Dict[str, Tuple[str, str, int]]:
        """
        Builds a mapping of instance_id:interface_index -> (hypervisor, ovs_bridge, ovs_port_number)
        Waits for IPv4 addresses to be assigned.
        """
        logger.info(
            f"Building port map (expecting ~{expected_count} nodes, timeout={timeout}s)..."
        )
        start_time = time.time()
        port_map = {}

        while time.time() - start_time < timeout:
            current_map = self._scan_ports()
            port_map = current_map

            got = len(port_map)
            # If we expect N nodes, we expect at least N eth0 interfaces approx.
            # Ideally each node has >=1 interface.
            if expected_count > 0 and got >= expected_count:
                logger.info(
                    f"Port map complete: found {got}/{expected_count} interfaces."
                )
                return port_map

            # Log progress periodically?
            if int(time.time() - start_time) % 5 == 0:
                logger.info(f"Waiting for ports... found {got}/{expected_count}")

            time.sleep(2)

        logger.warning(
            f"Timeout waiting for ports. Found {len(port_map)}/{expected_count}."
        )
        return port_map

    def _scan_ports(self) -> Dict[str, Tuple[str, str, int]]:
        """Internal scan for port mapping."""
        port_map = {}
        hypervisors = CONFIG.get("hypervisors", {})
        logger.debug(
            f"Scanning ports. Configured hypervisors: {list(hypervisors.keys())}"
        )

        # 1. Collect all instances to check
        targets = []  # List of (remote_alias, instance_dict)

        if hypervisors:
            for remote in hypervisors:
                try:
                    res = incus_client.executor.run(
                        ["incus", "list", f"{remote}:", "--format=json"],
                        check=True,
                        capture_output=True,
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
                    i
                    for i in instances
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
            if not name:
                continue

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
                int(instance_id)  # Validate it is int
            except ValueError:
                continue

            for iface, data in net_state.items():
                if data.get("type") != "broadcast":
                    continue

                # Check IPv4 (Optional now, to allow rule generation before DHCP)
                ipv4 = None
                for addr in data.get("addresses", []):
                    if addr.get("family") == "inet":
                        ipv4 = addr.get("address")
                        break

                # if not ipv4:
                #    logger.debug(f"No IPv4 for {name} {iface}")
                #    # continue  <-- relaxed

                # Check ethX
                if not iface.startswith("eth"):
                    continue

                try:
                    # Parse index
                    idx = int(re.findall(r"\d+", iface)[0])

                    # Get host device
                    host_dev = data.get("host_name")
                    if not host_dev:
                        continue

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

                    if not br:
                        continue
                    if ofport is None or ofport < 0:
                        continue

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

    def _resolve_remote_port(
        self, hypervisor: str, interface: str
    ) -> Tuple[Optional[str], Optional[int]]:
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
                ssh_cmd = [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=5",
                    ssh_addr,
                ] + cmd_list
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
        ofport_str = run_ssh(
            ["sudo", "ovs-vsctl", "get", "Interface", interface, "ofport"]
        )

        try:
            ofport = int(ofport_str)
            return br_name, ofport
        except (ValueError, TypeError):
            return br_name, None

    def _get_hypervisor_ssh_addr(self, hypervisor: str) -> str:
        """Returns the SSH host/IP for a hypervisor."""
        hv_config = CONFIG.get("hypervisors", {}).get(hypervisor, {})
        return hv_config.get("host") or hv_config.get("ip") or hypervisor
