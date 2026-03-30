import logging
import os
import time
import functools
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from meco.config.loader import CONFIG
from meco.infra.executors import LocalExecutor, SshExecutor
from meco.infra.incus import IncusClient
from meco.network.manager import NetworkManager

from meco.emulation import generator
from meco.emulation.scheduler import Scheduler, TopologyScheduler

from meco.utils.logger import setup_logging

logger = setup_logging("meco.lifecycle")

# Dependency initialization
# We keep these for local operations or as defaults
local_executor = LocalExecutor()
local_incus_client = IncusClient(local_executor)
net_manager = NetworkManager()

ACTIVITY_FLAG = "/tmp/meco_activity"


class LifecycleManager:
    """
    Orchestrates the deployment, start, and stop of emulations.
    """

    def __init__(self):
        self.clients = {"local": local_incus_client}
        self.node_locations = {}  # name -> remote_alias (or None for local)
        self.topology_scheduler = None
        self._init_clients()

    def _init_clients(self):
        hypervisors = CONFIG.get("hypervisors", {})

        # 1. Build Client Map
        for name, data in hypervisors.items():
            ip = data.get("ip")
            if not ip:
                continue

            # Assume user 'ubuntu' and standard port for now, or add to config later
            logger.info(f"Initializing SSH client for {name} ({ip})...")
            executor = SshExecutor(host=ip, user="ubuntu")
            self.clients[name] = IncusClient(executor)

        # 2. Build Instance Location Map
        # hypervisors: { 'hv1': { 'instances': ['1-Sat', ...], ... } }
        for hv_name, data in hypervisors.items():
            for inst in data.get("instances") or []:
                self.node_locations[inst] = hv_name

    def start_emulation(self, topology_data, dry_run=False):
        if os.path.exists(ACTIVITY_FLAG) and not dry_run:
            raise RuntimeError("Emulation already running.")

        if dry_run:
            logger.info("Dry run complete. No resources created.")
        if dry_run:
            logger.info("Dry run complete. No resources created.")
            yield "Dry run complete. No resources created."
            yield {"success": True, "flows_inserted": False, "dry_run": True}
            return

        # 1. Setup Infra
        logger.info("Setting up infrastructure...")
        yield "Setting up infrastructure..."
        net_manager.setup_infrastructure()

        # 2. Deploy Nodes
        try:
            yield "Deploying nodes..."
            self._deploy_nodes(topology_data)
        except Exception as e:
            logger.error(f"Deployment failed: {e}")
            self.stop_emulation(force=True)
            raise

        # 3. Configure Network (Port Mapping & Flows)
        try:
            yield "Configuring network flows..."
            flows_inserted = self._configure_network(topology_data)
        except Exception as e:
            logger.error(f"Network configuration failed: {e}")
            self.stop_emulation(force=True)
            raise

        # 4. Set Activity Flag
        with open(ACTIVITY_FLAG, "w") as f:
            f.write(str(time.time()))

        # 5. Start dynamic topology scheduler
        try:
            yield "Starting dynamic topology updates..."
            self.topology_scheduler = TopologyScheduler(topology_data, net_manager)
            self.topology_scheduler.start()
        except Exception as e:
            logger.error(f"Failed to start TopologyScheduler: {e}")

        yield {"success": True, "flows_inserted": flows_inserted, "dry_run": False}

    def stop_emulation(self, force=True):
        if not os.path.exists(ACTIVITY_FLAG) and not force:
            logger.warning("No active emulation to shut down.")
            yield "No active emulation to shut down."
            yield False
            return

        logger.info("Shutting down emulation...")
        yield "Identifying active instances..."

        if hasattr(self, "topology_scheduler") and self.topology_scheduler:
            logger.info("Stopping dynamic scheduler...")
            self.topology_scheduler.stop()

        # 1. Delete Instances
        yield "Stopping instances..."
        self._delete_all_instances(force)

        # 2. Teardown Network
        yield "Tearing down network infrastructure..."
        net_manager.teardown_infrastructure()

        # 3. Clear Flag
        yield "Cleaning up..."
        if os.path.exists(ACTIVITY_FLAG):
            try:
                os.remove(ACTIVITY_FLAG)
            except FileNotFoundError:
                pass

        logger.info("Shutdown complete.")
        yield "Shutdown complete."
        yield True

    def _deploy_nodes(self, data):
        nodes = data.get("nodes", [])

        tasks = []
        node_map = {}  # Map instance name -> remote alias (or None for local)

        # 1. Identify and Schedule Unassigned Nodes
        unassigned_nodes = []
        for node in nodes:
            name = f"{node['id']}-{node['type']}"
            if name not in self.node_locations:
                unassigned_nodes.append(name)

        if unassigned_nodes:
            logger.info(f"Scheduling {len(unassigned_nodes)} unassigned instances...")
            # Use only remote clients if available, otherwise fallback to local
            all_hvs = list(self.clients.keys())
            remote_hvs = [h for h in all_hvs if h != "local"]

            available_hvs = remote_hvs if remote_hvs else all_hvs
            assignments = Scheduler.schedule(unassigned_nodes, available_hvs)

            for inst, target in assignments.items():
                # Convert "local" string back to None for internal consistency
                final_target = None if target == "local" else target
                self.node_locations[inst] = final_target
                logger.info(f"  -> Assigned {inst} to {final_target or 'local'}")

        for node in nodes:
            name = f"{node['id']}-{node['type']}"
            is_vm = node["type"].lower() == "groundstation"

            # Identify where this node goes
            remote = self.node_locations.get(name)
            node_map[name] = remote

            # Generate Cloud Init
            cloud_content = generator.generate_cloudinit(node)
            cloud_file = f"/tmp/{name}-cloud.yaml"
            with open(cloud_file, "w") as f:
                f.write(cloud_content)

            tasks.append(
                functools.partial(
                    self._launch_node,
                    name=name,
                    is_vm=is_vm,
                    local_cloud_file=cloud_file,
                )
            )

        # 1. Prime Connections (Avoid SSH ControlPath race conditions)
        logger.info("Priming connections to hypervisors...")
        unique_remotes = set([r for r in node_map.values() if r])
        for remote in unique_remotes:
            client = self.clients.get(remote)
            if client:
                # Run a lightweight command to establish the ControlMaster connection synchronously
                client.check_installed()

        # 2. Launch all nodes in parallel (Fire & Forget)
        # SshExecutor with background=True means this returns almost instantly
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(t) for t in tasks]
            for fut in as_completed(futures):
                fut.result()

        # 3. Wait for all nodes to be ready (Bulk Check)
        self._wait_for_deployment(node_map)

    def _wait_for_deployment(self, node_map):
        """
        Wait for instances to start, grouped by hypervisor to minimize remote calls.
        """
        logger.info("Waiting for all instances to reach Running state...")

        # Group by client
        client_batches = defaultdict(list)
        for name, remote in node_map.items():
            client = (
                self.clients.get(remote, local_incus_client)
                if remote
                else local_incus_client
            )
            client_batches[client].append(name)

        def wait_batch(client, names):
            if not client.wait_for_instances(names, "Running", timeout=300):
                raise RuntimeError(f"Timeout waiting for instances on client {client}")

        with ThreadPoolExecutor(max_workers=len(client_batches)) as pool:
            futures = [
                pool.submit(wait_batch, client, names)
                for client, names in client_batches.items()
            ]
            for fut in as_completed(futures):
                fut.result()

        logger.info("All instances are Running.")

    def _launch_node(self, name, is_vm, local_cloud_file):
        remote = self.node_locations.get(name)
        client = (
            self.clients.get(remote, local_incus_client)
            if remote
            else local_incus_client
        )

        # Determine paths
        final_cloud_file = local_cloud_file

        if remote:
            # Upload file if remote
            final_cloud_file = f"/tmp/{name}-cloud.yaml"  # Target path on remote
            logger.info(
                f"Uploading {local_cloud_file} to {remote}:{final_cloud_file}..."
            )
            if not client.executor.upload_file(local_cloud_file, final_cloud_file):
                raise RuntimeError(
                    f"Failed to upload cloud-init for {name} to {remote}"
                )

        profile_base = (
            net_manager.profile_vm if is_vm else net_manager.profile_container
        )

        image = (
            CONFIG["defaults"]["image_vm"]
            if is_vm
            else CONFIG["defaults"]["image_container"]
        )

        # Tag the instance for identifying ownership
        # Generate Deterministic MAC
        try:
            node_id = name.split("-")[0]
            # Primary interface (eth0)
            mac_addr = generator.generate_mac(node_id, port_index=0)
        except Exception:
            mac_addr = None

        instance_config = {"user.meco": "true"}
        if mac_addr:
            instance_config["volatile.eth0.hwaddr"] = mac_addr

        # Prepare Devices
        devices = {}

        if "Satellite" in name:
            # Interfaces 1-4: Inter-Satellite Links (Bridged to unmanaged br-int)
            for i in range(1, 5):
                if_name = f"isl{i}"
                mac = generator.generate_mac(node_id, port_index=i)
                devices[if_name] = {
                    "type": "nic",
                    "nictype": "bridged",
                    "parent": net_manager.bridge_internal,
                    "hwaddr": mac,
                    "name": if_name,
                }

            # Interface 5: Ground Link (Bridged)
            if_name = "gsl"
            mac = generator.generate_mac(node_id, port_index=5)
            devices[if_name] = {
                "type": "nic",
                "nictype": "bridged",
                "parent": net_manager.bridge_internal,
                "hwaddr": mac,
                "name": if_name,
            }

        else:
            # For Terminals and GroundStations
            if_name = "gsl"
            mac = generator.generate_mac(node_id, port_index=1)
            devices[if_name] = {
                "type": "nic",
                "nictype": "bridged",
                "parent": net_manager.bridge_internal,
                "hwaddr": mac,
                "name": if_name,
            }

        logger.info(
            f"Launching {name} on {remote or 'local'} with {len(devices)} interfaces"
        )
        # Note: launching with background=True (implied by SshExecutor changes)
        if not client.launch_instance(
            image=image,
            name=name,
            profiles=[profile_base],
            is_vm=is_vm,
            cloud_init_file=final_cloud_file,
            config=instance_config,
            devices=devices,
        ):
            raise RuntimeError(f"Failed to launch {name} on {remote or 'local'}")

    def _delete_all_instances(self, force):
        if not hasattr(self, "clients"):
            self.clients = {"local": local_incus_client}
            self._init_clients()

        def delete_on_client(item):
            name, client = item
            logger.info(f"Checking for instances to clean up on {name}...")
            try:
                instances = client.list_instances()
                logger.debug(
                    f"Instances on {name}: {[i.get('name') for i in instances]}"
                )
                to_delete = []
                for i in instances:
                    # Log config for debugging if needed (at debug level)
                    logger.debug(
                        f"Instance {i.get('name')} config: {i.get('config', {})}"
                    )
                    is_meco = i.get("config", {}).get("user.meco") == "true"
                    if is_meco:
                        to_delete.append(i["name"])

                logger.info(
                    f"Found {len(to_delete)} Meco instances on {name}: {to_delete}"
                )

                for inst in to_delete:
                    logger.info(f"Deleting {inst} on {name}...")
                    if client.delete_instance(inst, force):
                        logger.info(f"Deleted {inst} on {name}")
                    else:
                        logger.error(f"Failed to delete {inst} on {name}")
            except Exception as e:
                logger.warning(f"Error cleaning up on {name}: {e}")

        logger.info(f"Triggering cleanup on clients: {list(self.clients.keys())}")
        # Run sequentially for debugging
        for item in self.clients.items():
            delete_on_client(item)
        logger.info("Cleanup iteration complete.")

    def _configure_network(self, data):
        logger.info("Configuring network flows (OF13)...")

        # New method handles waiting for IPs and flow generation
        hv_rules = net_manager.generate_visibility_rules(data)

        if not hv_rules:
            logger.warning("No flows generated or port map empty.")
            # The new method returns empty dict on failure/empty map
            # We should probably return False if it really failed, but empty might be valid for no visibility
            # However, if port map failed, it logged error.
            return False

        # 3. Apply Flows
        flows_inserted = False
        for hv, bridges in hv_rules.items():
            for br, rules in bridges.items():
                if rules:
                    logger.info(f"Applying {len(rules)} rules to {hv or 'local'}:{br}")
                    target = hv if hv else None
                    net_manager.apply_flows(br, rules, target=target)
                    flows_inserted = True

        # 4. Setup Satellite MACVLANs
        try:
            logger.info("Setting up Satellite-side MACVLANs...")
            self._setup_satellite_macvlans(data)
        except Exception as e:
            logger.warning(f"Satellite MACVLAN setup semi-failed: {e}")

        # 5. Setup Terminal Data Links (IPs and ARP)
        try:
            logger.info("Setting up Terminal data interfaces...")
            self._setup_terminal_data_links(data)
        except Exception as e:
            logger.warning(f"Terminal data link setup semi-failed: {e}")

        return flows_inserted

    def _setup_satellite_macvlans(self, data):
        """
        Creates MACVLAN interfaces inside Satellites for each visible Terminal.
        Format: gsl-<terminal_id>
        """
        nodes = data.get("nodes", [])
        node_type_map = {}
        for n in nodes:
            try:
                node_type_map[int(n["id"])] = n["type"].lower()
            except (ValueError, KeyError):
                continue

        links = net_manager._collect_topology_links(data)

        # Group by satellite
        sat_links = defaultdict(list)
        for a, b in links:
            a_type = node_type_map.get(a)
            b_type = node_type_map.get(b)

            if a_type == "satellite" and b_type == "terminal":
                sat_links[a].append(b)
            elif b_type == "satellite" and a_type == "terminal":
                sat_links[b].append(a)

        for sat_id, term_ids in sat_links.items():
            sat_name = f"{sat_id}-Satellite"
            remote = self.node_locations.get(sat_name)
            client = self.clients.get(remote, local_incus_client)

            for term_id in term_ids:
                iface_name = f"gsl-{term_id}"
                # Port index 100 + term_id to avoid collision with physical ethX
                mac = generator.generate_mac(sat_id, port_index=100 + term_id)

                logger.info(
                    f"Creating MACVLAN {iface_name} in {sat_name} for Terminal {term_id} (MAC: {mac})"
                )

                # Command to create MACVLAN:
                # ip link add gsl-2 link gsl type macvlan mode bridge
                cmds = [
                    [
                        "ip",
                        "link",
                        "add",
                        iface_name,
                        "link",
                        "gsl",
                        "type",
                        "macvlan",
                        "mode",
                        "bridge",
                    ],
                    ["ip", "link", "set", iface_name, "address", mac],
                    ["ip", "link", "set", iface_name, "up"],
                ]

                # Automated Data Plane IP
                sat_ip = generator.generate_ip(
                    sat_id, term_id, True, "Satellite", "Terminal"
                )
                term_ip = generator.generate_ip(
                    sat_id, term_id, False, "Satellite", "Terminal"
                )
                term_mac = generator.generate_mac(term_id, 1)  # gsl on Terminal

                if sat_ip and term_ip:
                    cmds.append(
                        ["ip", "addr", "add", f"{sat_ip}/24", "dev", iface_name]
                    )
                    # Add static ARP for Terminal to minimize noise/flooding
                    cmds.append(
                        [
                            "ip",
                            "neigh",
                            "add",
                            term_ip,
                            "lladdr",
                            term_mac,
                            "dev",
                            iface_name,
                        ]
                    )

                for cmd in cmds:
                    # Use absolute path to incus or rely on PATH
                    full_cmd = ["incus", "exec", sat_name, "--"] + cmd
                    res = client.executor.run(
                        full_cmd, check=False, capture_output=True
                    )
                    if res.returncode != 0:
                        logger.error(f"Command failed: {full_cmd} -> {res.stderr}")
                    else:
                        logger.debug(f"Command succeeded: {full_cmd}")

    def _setup_terminal_data_links(self, data):
        """
        Configures static IPs and ARP neighbors on Terminal data interfaces (gsl).
        """
        nodes = data.get("nodes", [])
        node_id_to_type = {n["id"]: n["type"] for n in nodes}
        node_id_to_name = {n["id"]: f"{n['id']}-{n['type']}" for n in nodes}
        node_hv_map = self.node_locations

        links = net_manager._collect_topology_links(data)

        for src_id, dst_id in links:
            # We are interested in Ground Links: Satellite <-> Terminal
            src_type = node_id_to_type.get(src_id, "").lower()
            dst_type = node_id_to_type.get(dst_id, "").lower()

            # Determine Terminal and Satellite
            if "terminal" in src_type and "satellite" in dst_type:
                term_id, sat_id = src_id, dst_id
            elif "terminal" in dst_type and "satellite" in src_type:
                term_id, sat_id = dst_id, src_id
            else:
                continue

            term_name = node_id_to_name.get(term_id)
            hv = node_hv_map.get(term_name)
            if not hv:
                # If not in config instances, check if it's local
                hv = "local"

            client = self.clients.get(hv)
            if not client:
                continue

            # Deterministic IPs and Peer MAC
            term_ip = generator.generate_ip(
                term_id, sat_id, True, "Terminal", "Satellite"
            )
            sat_ip = generator.generate_ip(
                term_id, sat_id, False, "Terminal", "Satellite"
            )
            sat_mac = generator.generate_mac(
                sat_id, 100 + term_id
            )  # MACVLAN MAC on Satellite

            if term_ip and sat_ip:
                # gsl is the default data interface on Terminals
                cmds = [
                    ["ip", "addr", "add", f"{term_ip}/24", "dev", "gsl"],
                    ["ip", "link", "set", "gsl", "up"],
                    ["ip", "neigh", "add", sat_ip, "lladdr", sat_mac, "dev", "gsl"],
                ]

                for cmd in cmds:
                    full_cmd = ["incus", "exec", term_name, "--"] + cmd
                    client.executor.run(full_cmd, check=False, capture_output=True)
