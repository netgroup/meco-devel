import logging
from meco.config.loader import CONFIG
from meco.infra.executors import LocalExecutor
from meco.infra.incus import IncusClient
from meco.network import ovs
from meco.emulation import generator
import re
import time
import json
from collections import defaultdict
from typing import List, Dict, Tuple, Optional, Set
from meco.utils.logger import setup_logging
import subprocess
import shlex

logger = setup_logging("meco.network.manager")

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
        self.port_map = {}

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
                    logger.warning(
                        f"Network {self.bridge_internal} creation via Incus skipped (using OVS manual)."
                    )
                # Actually, we WANT to use manual OVS to avoid DNS conflicts on ISL links.
                # So we should validly use ovs.add_bridge
                if not ovs.add_bridge(self.bridge_internal):
                    raise RuntimeError(
                        f"Failed to create OVS bridge {self.bridge_internal}"
                    )

            else:
                logger.info(
                    f"Setting up distributed infrastructure on {len(hypervisors)} remotes..."
                )

                # Management network (must be created by Incus or exist)
                # We assume incusbr0 exists or we default to a managed one?
                # For now we rely on existing.
                needed_nets = [self.bridge_internal, self.bridge_tunnel]

                for remote in hypervisors:
                    full_name = f"{remote}:{self.bridge_internal}"
                    logger.info(f"Creating {full_name} (OVS)...")
                    if not ovs.add_bridge(self.bridge_internal, target=remote):
                        raise RuntimeError(f"Failed to create bridge {full_name}")

                    # Tunnel bridge also manual?
                    if not ovs.add_bridge(self.bridge_tunnel, target=remote):
                        raise RuntimeError(
                            f"Failed to create tunnel bridge on {remote}"
                        )

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
                except Exception:
                    pass

            incus_client.delete_network(f"{target_prefix}{br}")
            # Also try OVS delete in case it was manual
            # ovs.del_bridge? We don't have it yet, but del_flows is fine.
            # We should remove the bridge if we created it manually.
            # But let's leave it for now or use raw command?
            # Ideally implemented in ovs.py. For now we assume verify_network cleans up profiles.

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

        # Add eth0 to MANAGEMENT network (incusbr0 default)
        # We need a config option for this? defaults.get('mgmt_bridge', 'incusbr0')
        mgmt_bridge = self.defaults.get("mgmt_bridge", "incusbr0")

        incus_client.add_profile_device(
            p_cnt, "eth0", "nic", "nictype=bridged", f"parent={mgmt_bridge}"
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
        Sets up a single VXLAN tunnel port for flow-based tunneling.
        Port name: vxlan-overlay
        """
        if not current_hv:
            return  # Local only setup handled differently or ignored?

        logger.info(f"Setting up flow-based tunnel on {current_hv}...")

        # Create single port with remote_ip=flow and key=flow
        # This allows us to set tun_dst and tun_id in flow rules
        ovs.add_vxlan_port(
            self.bridge_tunnel, "vxlan-overlay", "flow", key="flow", target=current_hv
        )

    def generate_base_rules(self, bridge: str) -> List[str]:
        """
        Returns static initialized rules for br-int or br-tun.
        Fail-Safe: Default Drop. Using multiple tables for scalability.
        """
        rules = []
        if bridge == self.bridge_internal:
            # --- br-int ---
            # [Table 0] Classification
            rules.append("table=0,priority=0,actions=drop")
            # DHCP Bypass
            rules.append("table=0,priority=1000,udp,tp_src=68,tp_dst=67,actions=NORMAL")
            rules.append("table=0,priority=1000,udp,tp_src=67,tp_dst=68,actions=NORMAL")
            # Send ARP to Table 10
            rules.append("table=0,priority=1,arp,actions=resubmit(,10)")
            # Send IP to Table 20 (Unicast Forwarding)
            rules.append("table=0,priority=1,ip,actions=resubmit(,20)")

            # [Table 10] ARP Proxy (Default Drop)
            rules.append("table=10,priority=0,actions=drop")

            # [Table 20] Unicast Forwarding (Default Drop)
            rules.append("table=20,priority=0,actions=drop")

        elif bridge == self.bridge_tunnel:
            # --- br-tun ---
            # [Table 0] Ingress / Egress Classification
            rules.append("table=0,priority=0,actions=drop")
            # Egress (from br-int) -> Table 30
            rules.append("table=0,priority=1,in_port=patch-int,actions=resubmit(,30)")
            # Ingress (from vxlan-overlay) -> output:patch-int
            rules.append(
                "table=0,priority=1,in_port=vxlan-overlay,actions=output:patch-int"
            )

            # [Table 30] Encapsulation (Default Drop)
            rules.append("table=30,priority=0,actions=drop")

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

    def _get_link_cookie(self, src_id: int, dst_id: int) -> str:
        """Generates a unique hex cookie for the un-directed link pair starting with 0x99."""
        link_id = (min(src_id, dst_id) << 12) | max(src_id, dst_id)
        return f"0x99{link_id:06x}"

    def apply_topology_delta(self, links_to_add: set, links_to_remove: set, topo: dict):
        """
        Applies changes to topology incrementally based on deltas.
        """
        # 1. Delete removed links
        for src, dst in links_to_remove:
            cookie = self._get_link_cookie(src, dst)
            logger.info(f"Deleting broken link {src}->{dst} rules with cookie={cookie} from all relevant bridges...")
            hypervisors = CONFIG.get("hypervisors", {})
            targets = list(hypervisors.keys()) if hypervisors else [None]
            for hv in targets:
                for br in [self.bridge_internal, self.bridge_tunnel]:
                    try:
                        ovs.del_flows_by_cookie(br, f"{cookie}/-1", target=hv)
                    except Exception as e:
                        logger.debug(f"Failed to delete flow with cookie {cookie} on {hv}/{br}: {e}")

        # 2. Add new links
        if links_to_add:
            logger.info(f"Generating new rules for {len(links_to_add)} added links...")
            rules_per_hv = self.generate_visibility_rules(topo, links_to_add)
            for hv, br_rules in rules_per_hv.items():
                for br, flows in br_rules.items():
                    target_remote = hv if hv and hv != "local" else None
                    logger.info(f"Inserting {len(flows)} new OpenFlow rules to {br} on {target_remote or 'local'}...")
                    for i, rule in enumerate(flows):
                        logger.info(f"  -> Rule [{i+1}/{len(flows)}]: {rule}")
                        ovs.add_flow(br, rule, target=target_remote)

    def generate_visibility_rules(
        self, topo: dict, links: set
    ) -> Dict[str, Dict[str, List[str]]]:
        """
        Generates OpenFlow rules for MAC-based switching.
        Logic:
            - Iterate all links.
            - For each link Pair (A, B):
              - Generate Rule on A's HV: Allow SrcMAC_A -> DstMAC_B.
              - Generate Rule on B's HV: Allow SrcMAC_A -> DstMAC_B.
              - If Inter-Hypervisor:
                - Source HV: Output to vxlan-overlay (set tun_dst=DestIP, tun_id=SatID).
                - Dest HV: Ingress handled by default rule (vxlan -> patch-int),
                  br-int rule handles forwarding to Port_B.
        """
        if not self.port_map:
            self.port_map = self.build_port_map(
                expected_count=len(topo.get("nodes", [])), timeout=120
            )

        port_map = self.port_map

        if not port_map:
            logger.error("No port map available. Cannot generate visibility rules.")
            return {}

        node_type = {}
        for n in topo.get("nodes", []):
            try:
                nid = int(n.get("id"))
                node_type[nid] = str(n.get("type", "")).lower()
            except Exception:
                continue

        # Helper: Get Hypervisor IP
        hypervisors_config = CONFIG.get("hypervisors", {})

        def get_hv_ip(hv_name):
            if not hv_name or hv_name == "local":
                return None
            data = hypervisors_config.get(hv_name, {})
            return data.get("ip") or data.get("host")

        # Helper: Resolve Node Info (Context Aware)
        def get_node_info(nid, peer_id):
            # Determine appropriate interface index
            # Rules:
            # - Terminals/GS -> Satellite: use gsl (idx 1 on Term/GS)
            # - Satellite -> Terminals/GS: use gsl (idx 5 on Sat)
            # - Satellite -> Satellite: use isl1-4 (idx 1-4)

            my_type = node_type.get(nid, "")
            peer_type = node_type.get(peer_id, "")

            target_idx = 1  # Default to gsl (Data)

            if "satellite" in my_type:
                if "satellite" in peer_type:
                    target_idx = 1  # ISL
                else:
                    target_idx = 5  # Ground Link

            # Look up in port_map "nid:idx"
            pm = port_map.get(f"{nid}:{target_idx}")

            # Fallback: If specific interface not found, try finding ANY interface on br-int
            # This handles cases where maybe gsl failed to map but gsl-idx1 is there
            if not pm and "satellite" in my_type and target_idx == 5:
                logger.debug(f"Missing gsl for Sat {nid}, checking fallbacks...")
                # Try index 1 (legacy or other data interface)
                pm = port_map.get(f"{nid}:1")

            if not pm:
                return None

            # pm is (hv, br, port, ip)
            hv, br, port, ip = pm[0], pm[1], pm[2], pm[3]
            final_idx = target_idx
            if port_map.get(f"{nid}:{target_idx}") != pm:
                if port_map.get(f"{nid}:1") == pm:
                    final_idx = 1
                elif port_map.get(f"{nid}:0") == pm:
                    final_idx = 0

            # --- SATELLITE MACVLAN LOGIC ---
            # If a Satellite is talking to a Terminal, it uses a terminal-specific MACVLAN.
            # These are generated with port_index = 100 + Terminal_ID.
            final_mac_idx = final_idx
            if "satellite" in my_type and "terminal" in peer_type:
                final_mac_idx = 100 + peer_id

            mac = generator.generate_mac(nid, final_mac_idx)

            # --- SATELLITE DATA IP LOGIC ---
            # If we are on a data link (index >= 1) between Sat and Terminal,
            # we use a deterministic data IP instead of the management IP (pm[3]).
            if final_idx >= 1 and (
                ("satellite" in my_type and "terminal" in peer_type)
                or ("terminal" in my_type and "satellite" in peer_type)
            ):
                ip = generator.generate_ip(nid, peer_id, True, my_type, peer_type)

            return (mac, hv, br, port, ip)

        # Result structure: hv -> bridge -> rules
        rules = defaultdict(lambda: defaultdict(list))

        for src_id, dst_id in links:
            cookie_hex = self._get_link_cookie(src_id, dst_id)
            src_info = get_node_info(src_id, dst_id)
            dst_info = get_node_info(dst_id, src_id)

            if not src_info or not dst_info:
                logger.warning(f"Skipping link {src_id}->{dst_id}: missing port info")
                continue

            s_mac, s_hv, s_br, s_port, s_ip = src_info
            d_mac, d_hv, d_br, d_port, d_ip = dst_info

            # Determine "Link ID" / "Satellite ID" for tunneling
            # Prefer Satellite ID if one is Satellite
            s_type = node_type.get(src_id, "")
            d_type = node_type.get(dst_id, "")

            # Simple heuristic: Use the ID of the Satellite node, or Src ID if both/neither
            tunnel_key = src_id
            if "satellite" in s_type:
                tunnel_key = src_id
            elif "satellite" in d_type:
                tunnel_key = dst_id

            for (
                A_id,
                A_mac,
                A_hv,
                A_br,
                A_port,
                B_id,
                B_mac,
                B_hv,
                B_br,
                B_port,
                B_ip,
            ) in [
                (
                    src_id,
                    s_mac,
                    s_hv,
                    s_br,
                    s_port,
                    dst_id,
                    d_mac,
                    d_hv,
                    d_br,
                    d_port,
                    d_ip,
                ),
                (
                    dst_id,
                    d_mac,
                    d_hv,
                    d_br,
                    d_port,
                    src_id,
                    s_mac,
                    s_hv,
                    s_br,
                    s_port,
                    s_ip,
                ),
            ]:
                # Rule 1: Allow A -> B (Data)

                # 1. On Source HV (A_hv)
                if A_hv == B_hv:
                    # Local switching (same HV)
                    # br-int: dl_src=A, dl_dst=B -> output:B_port
                    rules[A_hv]["br-int"].append(
                        f"cookie={cookie_hex},table=20,priority=100,dl_src={A_mac},dl_dst={B_mac},actions=output:{B_port}"
                    )
                else:
                    # Remote switching
                    # 1. Egress on A_hv (br-int -> br-tun -> VXLAN)

                    # br-int: Send to patch-tun
                    rules[A_hv]["br-int"].append(
                        f"cookie={cookie_hex},table=20,priority=100,dl_src={A_mac},dl_dst={B_mac},actions=output:patch-tun"
                    )

                # 2. On Source HV (A_hv) - Restricted ARP Proxy
                # If we know B's IP, allow A to resolve it via Proxy ARP
                if B_ip:
                    rules[A_hv]["br-int"].append(
                        f"cookie={cookie_hex},table=10,priority=150,arp,in_port={A_port},arp_tpa={B_ip},arp_op=1,"
                        f"actions=set_field:{B_mac}->dl_dst,resubmit(,20)"
                    )

                # 3. Tunnel Encapsulation (if remote)
                if A_hv != B_hv:
                    # br-tun: Encapsulate
                    # Match: patch-int + MACs
                    # Action: set_field:RemoteIP->tun_dst, set_field:LinkID->tun_id, output:vxlan-overlay
                    remote_ip = get_hv_ip(B_hv)
                    if remote_ip:
                        rules[A_hv]["br-tun"].append(
                            f"cookie={cookie_hex},table=30,priority=100,in_port=patch-int,dl_src={A_mac},dl_dst={B_mac},"
                            f"actions=set_field:{remote_ip}->tun_dst,set_field:0x{tunnel_key:x}->tun_id,output:vxlan-overlay"
                        )

                # 2. On Dest HV (B_hv)
                if A_hv != B_hv:
                    # Ingress is handled by default rule on br-tun (vxlan -> patch-int)
                    # We just need to guide it on br-int

                    # br-int: dl_src=A, dl_dst=B -> output:B_port
                    rules[B_hv]["br-int"].append(
                        f"cookie={cookie_hex},table=20,priority=100,dl_src={A_mac},dl_dst={B_mac},actions=output:{B_port}"
                    )

        return dict(rules)

    def _collect_topology_links(
        self, topo: dict, epoch_time: int = 0
    ) -> List[Tuple[int, int]]:
        links = []
        for section in ("visibility-constellation", "visibility-ground"):
            for snap in topo.get(section, []):
                if snap.get("time") == epoch_time:
                    for conn in snap.get("connection", []):
                        src = conn.get("source")
                        dst = conn.get("destination")
                        if src is not None and dst is not None:
                            links.append((src, dst))
        return links

    def build_port_map(
        self, expected_count: int = 0, timeout: int = 120
    ) -> Dict[str, Tuple[str, str, int, Optional[str]]]:
        """
        Builds a mapping of instance_id:interface_index -> (hypervisor, ovs_bridge, ovs_port_number, ip)
        Waits for IPv4 addresses to be assigned.
        """
        logger.info(
            f"Building port map (expecting ~{expected_count} nodes, timeout={timeout}s)..."
        )
        start_time = time.time()
        port_map = {}

        while time.time() - start_time < timeout:
            current_map, pending = self._scan_ports()
            port_map = current_map

            got = len(port_map)
            # If we expect N nodes, we expect at least N eth0 interfaces approx.
            # Ideally each node has >=1 interface.
            if pending == 0 and expected_count > 0 and got >= expected_count:
                logger.info(
                    f"Port map complete: found {got}/{expected_count} interfaces."
                )
                self.port_map = port_map
                return port_map

            # If expected_count is 0, we still want to wait if we see pending IPs
            if pending == 0 and expected_count == 0 and got > 0:
                # Heuristic: if we found some nodes and no pending ips, maybe we are done?
                # Or we should wait a bit more?
                # Ideally expected_count should be passed.
                logger.info(f"Port map converged (count={got}).")
                self.port_map = port_map
                return port_map

            # Log progress periodically?
            if int(time.time() - start_time) % 5 == 0:
                logger.info(
                    f"Waiting for ports... found {got}/{expected_count} (pending IPs: {pending})"
                )

            time.sleep(2)

        logger.warning(
            f"Timeout waiting for ports. Found {len(port_map)}/{expected_count}."
        )
        self.port_map = port_map
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
        pending_ips = 0

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

                # Check IPv4 (Required for Proxy ARP)
                ipv4 = None
                for addr in data.get("addresses", []):
                    if addr.get("family") == "inet":
                        ipv4 = addr.get("address")
                        break

                # Interface index mapping logic
                if iface.startswith("isl"):
                    idx = int(re.findall(r"\d+", iface)[0])
                elif iface == "gsl":
                    # Heuristic: Satellite has gsl at index 5, Terminal at index 1
                    idx = 5 if "Satellite" in name else 1
                elif iface.startswith("eth"):
                    idx = int(re.findall(r"\d+", iface)[0])
                else:
                    continue

                try:
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

                    # Check IPv4 (Optional for unmanaged bridges)
                    ipv4 = None
                    for addr in data.get("addresses", []):
                        if addr.get("family") == "inet":
                            ipv4 = addr.get("address")
                            break

                    # If no IP, strictly check if we expect one
                    # On unmanaged bridges (br-int, br-tun), we do NOT expect Incus IPs
                    is_unmanaged = (
                        br == self.bridge_internal or br == self.bridge_tunnel
                    )

                    if not ipv4 and not is_unmanaged:
                        # Found interface but no IP on a managed bridge?
                        # This might be transient (waiting for DHCP)
                        pending_ips += 1
                        continue

                    # If unmanaged, ipv4=None is acceptable.

                    # Key: "id:index" -> (hv, br, ofport, ip)
                    # If remote is None, we need to know local hv name?
                    # Actually generate_of13 rules handles 'hv' as a key.
                    # If remote is None, we should use a default name or 'local'?
                    # In meco.py it used 'local' if no hypervisors.
                    hv_key = remote if remote else "local"

                    port_map[f"{instance_id}:{idx}"] = (hv_key, br, ofport, ipv4)
                    logger.debug(
                        f"Mapped {name}/{iface} -> {hv_key}/{br}/{ofport}/{ipv4}"
                    )

                except Exception:
                    continue

        return port_map, pending_ips

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

        # FIX: For MACVLAN/VEPA (e.g. gsl-X), the host_name is often the bridge itself (e.g. br-int).
        # ovs-vsctl port-to-br br-int fails. We must detect this.
        if not br_name:
            if interface == self.bridge_internal or interface == self.bridge_tunnel:
                br_name = interface
            else:
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
